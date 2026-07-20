import socket
import cv2
import numpy as np
import threading
import time
import tkinter as tk
from tkinter import scrolledtext, ttk
from datetime import datetime
from PIL import Image, ImageTk

from protocol import (
    TYPE_TEXT, TYPE_LIVE,
    send_message, send_text, FrameReceiver,
    pack_live_payload, unpack_live_payload,
    build_rtsp_request, parse_rtsp_message,
)

CONTROL_PORT = 7777
LIVE_PORT    = 7778

client_socket = None
running       = False
server_ip     = "127.0.0.1"

view_mode = "IDLE"

own_stream_id    = None
is_broadcasting  = False
own_cam          = None
upload_socket    = None
own_preview_buf  = None
own_preview_lock = threading.Lock()

cseq          = 0
pending_step  = None

watching_id          = None
auto_watch           = True   
remote_streams        = {}
remote_streams_lock   = threading.Lock()

#GUI
root = tk.Tk()
root.title("Live Stream Client")
root.geometry("520x640")
root.configure(bg="#1e1e2e")
root.update_idletasks()
root.maxsize(root.winfo_screenwidth(), root.winfo_screenheight())
root.minsize(420, 480)

BG     = "#1e1e2e"
FG     = "#e6e6e6"
MUTED  = "#9a9ab0"
ACCENT = "#5fb3ff"
GREEN  = "#6bdc8c"
RED    = "#ff6b6b"
BOX_BG = "#161622"

FONT_H1   = ("Segoe UI", 14, "bold")
FONT      = ("Segoe UI", 10)
FONT_MONO = ("Consolas", 9)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    log_box.insert(tk.END, f"{ts}  {msg}\n")
    log_box.see(tk.END)

def send_cmd(text):
    if client_socket is None:
        return
    try:
        send_text(client_socket, text)
    except Exception as e:
        log(f"Send error: {e}")

def update_buttons():
    connected = client_socket is not None and running
    if is_broadcasting:
        live_btn.config(text="End Broadcast", bg=RED, fg="#1e1e2e",
                         state='normal' if connected else 'disabled')
    else:
        live_btn.config(text="Go Live", bg=ACCENT if connected else "#3a3a4a",
                         fg="#1e1e2e" if connected else FG,
                         state='normal' if connected else 'disabled')
    with remote_streams_lock:
        have_others = len(remote_streams) > 0
    watch_btn.config(state='normal' if (have_others and connected) else 'disabled',
                      bg=ACCENT if (have_others and connected) else "#3a3a4a",
                      fg="#1e1e2e" if (have_others and connected) else FG)
    stop_watch_btn.config(state='normal' if view_mode == "WATCH" else 'disabled',
                           bg=RED if view_mode == "WATCH" else "#3a3a4a",
                           fg="#1e1e2e" if view_mode == "WATCH" else FG)
    connect_btn.config(state='disabled' if connected else 'normal',
                        bg="#3a3a4a" if connected else ACCENT,
                        fg=FG if connected else "#1e1e2e")

def refresh_watch_dropdown():
    with remote_streams_lock:
        ids = sorted(remote_streams.keys())
    current = watch_var.get()
    if ids:
        watch_dropdown['values'] = ids
        watch_dropdown.config(state='readonly')
        watch_var.set(current if current in ids else ids[0])
    else:
        watch_dropdown['values'] = ["(nobody live)"]
        watch_var.set("(nobody live)")
        watch_dropdown.config(state='disabled')
    update_buttons()

def maybe_auto_watch():
    global view_mode, watching_id
    if not auto_watch:
        return
    with remote_streams_lock:
        ids = sorted(remote_streams.keys())
    if ids:
        watching_id = ids[0]
        view_mode = "WATCH"
        watch_var.set(watching_id)
    else:
        watching_id = None
        view_mode = "IDLE"
    update_buttons()y

#go live
def toggle_own_live():
    global cseq, pending_step
    if is_broadcasting:
        stop_own_live()
    else:
        cseq += 1
        pending_step = "SETUP"
        log(f"Sending SETUP (CSeq {cseq})...")
        send_cmd(build_rtsp_request("SETUP", cseq))

def send_play():
    global cseq, pending_step
    cseq += 1
    pending_step = "PLAY"
    log(f"Sending PLAY (CSeq {cseq}, Session {own_stream_id})...")
    send_cmd(build_rtsp_request("PLAY", cseq, session=own_stream_id))

def begin_own_live():
    global is_broadcasting, own_cam, upload_socket
    if not own_stream_id:
        return
    own_cam = cv2.VideoCapture(0)
    if not own_cam.isOpened():
        log("No camera found.")
        send_cmd("STOPLIVE")
        return
    try:
        upload_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        upload_socket.connect((server_ip, LIVE_PORT))
        send_text(upload_socket, f"ID:{own_stream_id}")
    except Exception as e:
        log(f"Could not connect for upload: {e}")
        own_cam.release()
        own_cam = None
        send_cmd("STOPLIVE")
        return

    is_broadcasting = True
    log("You are live.")
    update_buttons()
    threading.Thread(target=camera_loop, daemon=True).start()

def camera_loop():
    global own_preview_buf, is_broadcasting
    while is_broadcasting:
        ret, frame = own_cam.read()
        if not ret:
            time.sleep(0.05)
            continue
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        with own_preview_lock:
            own_preview_buf = cv2.flip(rgb, 1)
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        try:
            send_message(upload_socket, TYPE_LIVE, buf.tobytes())
        except Exception:
            break
        time.sleep(1 / 20)
    try: own_cam.release()
    except Exception: pass
    try: upload_socket.close()
    except Exception: pass

def stop_own_live():
    global is_broadcasting, own_preview_buf, own_stream_id, cseq, pending_step
    is_broadcasting = False
    own_preview_buf = None
    cseq += 1
    pending_step = "TEARDOWN"
    log(f"Sending TEARDOWN (CSeq {cseq}, Session {own_stream_id})...")
    send_cmd(build_rtsp_request("TEARDOWN", cseq, session=own_stream_id))
    own_stream_id = None
    log("Broadcast ended.")
    update_buttons()

#watch
def watch_selected():
    global view_mode, watching_id, auto_watch
    sid = watch_var.get()
    if not sid or sid == "(nobody live)":
        return
    with remote_streams_lock:
        if sid not in remote_streams:
            log("That stream just ended.")
            return
    watching_id = sid
    view_mode = "WATCH"
    auto_watch = False  
    update_buttons()

def stop_watching():
    global view_mode, watching_id, auto_watch
    watching_id = None
    view_mode = "IDLE"
    auto_watch = True
    update_buttons()

#incoming messages
def handle_text(text):
    global own_stream_id, watching_id, view_mode, pending_step
    if text.startswith("STARTED:"):
        sid = text.split("STARTED:", 1)[1].strip()
        if sid == own_stream_id:
            return
        with remote_streams_lock:
            is_new = sid not in remote_streams
            remote_streams[sid] = remote_streams.get(sid)
        if is_new:
            log(f"{sid} went live.")
        root.after(0, refresh_watch_dropdown)
        root.after(0, maybe_auto_watch)
        return

    if text.startswith("ENDED:"):
        sid = text.split("ENDED:", 1)[1].strip()
        with remote_streams_lock:
            remote_streams.pop(sid, None)
        if watching_id == sid:
            watching_id = None
            view_mode = "IDLE"
            log(f"{sid} ended their stream.")
        root.after(0, refresh_watch_dropdown)
        root.after(0, maybe_auto_watch)
        return

    status, headers = parse_rtsp_message(text)
    if status is None:
        return
    log(f"Server: RTSP/1.0 {status}  (CSeq {headers.get('CSeq', '?')})")

    if status != "200":
        log(f"Request failed — {text.splitlines()[0]}")
        pending_step = None
        return

    if pending_step == "SETUP":
        own_stream_id = headers.get("Session")
        pending_step = None
        send_play()
    elif pending_step == "PLAY":
        pending_step = None
        threading.Thread(target=begin_own_live, daemon=True).start()
    elif pending_step == "TEARDOWN":
        pending_step = None

def receive_loop():
    global running
    receiver = FrameReceiver()
    while running:
        try:
            packet = client_socket.recv(65536)
            if not packet:
                raise ConnectionError("server closed connection")
            receiver.feed(packet)
            for msg_type, payload in receiver.pop_messages():
                if msg_type == TYPE_TEXT:
                    text = payload.decode(errors="ignore")
                    root.after(0, lambda t=text: handle_text(t))
                elif msg_type == TYPE_LIVE:
                    try:
                        sid, jpeg_bytes = unpack_live_payload(payload)
                    except Exception:
                        continue
                    if sid == own_stream_id:
                        continue
                    np_arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(np_arr, cv2.IMREAD_COLOR)
                    if frame is not None:
                        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        with remote_streams_lock:
                            remote_streams[sid] = rgb
        except Exception as e:
            if running:
                root.after(0, lambda: log(f"Connection lost: {e}"))
                running = False
                root.after(0, update_buttons)
            break

def draw_placeholder(target_canvas, msg):
    target_canvas.delete("all")
    w = target_canvas.winfo_width()  or 480
    h = target_canvas.winfo_height() or 220
    target_canvas.create_text(w // 2, h // 2, text=msg, fill="gray")
