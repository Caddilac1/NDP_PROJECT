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
    pack_live_payload,
    build_rtsp_response, parse_rtsp_message,
)

CONTROL_PORT = 7777
LIVE_PORT    = 7778
HOST         = '0.0.0.0'
SERVER_STREAM_ID = "SERVER"

is_stopped     = False
control_socket = None
live_socket    = None

clients      = []           
clients_lock = threading.Lock()

live_streams    = {}        
live_state_lock = threading.Lock()

live_previews     = {}      
live_preview_lock = threading.Lock()
selected_preview  = None   

server_cam          = None
server_broadcasting = False
server_paused       = False
own_preview_buf     = None
own_preview_lock    = threading.Lock()

#GUI
root = tk.Tk()
root.title("Live Stream Server")
root.geometry("420x420")
root.configure(bg="#1e1e2e")


BG     = "#1e1e2e"
FG     = "#e6e6e6"
MUTED  = "#9a9ab0"
ACCENT = "#5fb3ff"
RED    = "#ff6b6b"
BOX_BG = "#161622"

FONT_H1   = ("Segoe UI", 14, "bold")
FONT      = ("Segoe UI", 10)
FONT_MONO = ("Consolas", 9)

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    log_box.insert(tk.END, f"{ts}  {msg}\n")
    log_box.see(tk.END)

def broadcast(msg_type, payload):
    with clients_lock:
        dead = []
        for c in clients:
            try:
                send_message(c["conn"], msg_type, payload, lock=c["lock"])
            except Exception:
                dead.append(c)
        for c in dead:
            clients.remove(c)
    if dead:
        root.after(0, update_counts)

def broadcast_text(text):
    broadcast(TYPE_TEXT, text.encode())

def update_counts():
    with clients_lock:
        n_clients = len(clients)
    with live_state_lock:
        n_live = len(live_streams)
    status_var.set(f"{n_clients} connected   |   {n_live} live")


client_rows = {}  
def client_status_for(label):
    with live_state_lock:
        entry = live_streams.get(label)
    if entry is None:
        return "idle"
    return "live" if entry.get("state") == "PLAY" else "setting up"

def refresh_client_row(label):
    status = client_status_for(label)
    if label in client_rows and client_tree.exists(client_rows[label]):
        client_tree.item(client_rows[label], values=(label, status),
                          tags=(status.replace(" ", "_"),))
    else:
        client_rows[label] = client_tree.insert(
            "", tk.END, values=(label, status), tags=(status.replace(" ", "_"),))
def remove_client_row(label):
    iid = client_rows.pop(label, None)
    if iid is not None:
        try:
            client_tree.delete(iid)
        except Exception:
            pass

def clear_client_rows():
    client_rows.clear()
    for item in client_tree.get_children():
        client_tree.delete(item)

def on_client_select(event=None):
    global selected_preview
    sel = client_tree.selection()
    if sel:
        selected_preview = client_tree.item(sel[0], "values")[0]

def _draw_letterboxed(target_canvas, frame, box_w, box_h):
    frame_h, frame_w = frame.shape[:2]
    if frame_w <= 0 or frame_h <= 0 or box_w <= 0 or box_h <= 0:
        return None
    scale = min(box_w / frame_w, box_h / frame_h)
    new_w = max(1, int(frame_w * scale))
    new_h = max(1, int(frame_h * scale))
    x = (box_w - new_w) // 2
    y = (box_h - new_h) // 2
    img = Image.fromarray(frame).resize((new_w, new_h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    target_canvas.photo = photo
    target_canvas.delete("all")
    target_canvas.create_rectangle(0, 0, box_w, box_h, fill="#0d0d14", outline="")
    target_canvas.create_image(x, y, anchor='nw', image=photo)
    return photo

def update_preview_canvas():
    with live_preview_lock:
        frame = live_previews.get(selected_preview) if selected_preview else None
    w = preview_canvas.winfo_width()  or 420
    h = preview_canvas.winfo_height() or 240
    if frame is not None:
        try:
            _draw_letterboxed(preview_canvas, frame, w, h)
        except Exception:
            pass
    else:
        preview_canvas.delete("all")
        msg = f"waiting for {selected_preview}..." if selected_preview else "click a client below to preview"
        preview_canvas.create_text(w // 2, h // 2, text=msg, fill=MUTED, font=FONT)
    root.after(60, update_preview_canvas)

def update_own_canvas():
    with own_preview_lock:
        frame = own_preview_buf
    w = own_canvas.winfo_width()  or 420
    h = own_canvas.winfo_height() or 140
    if frame is not None:
        try:
            _draw_letterboxed(own_canvas, frame, w, h)
        except Exception:
            pass
    else:
        own_canvas.delete("all")
        msg = "starting camera..." if server_broadcasting else "your camera preview"
        own_canvas.create_text(w // 2, h // 2, text=msg, fill=MUTED, font=FONT)
    root.after(60, update_own_canvas)

def end_stream(stream_id, notify=True):
    with live_state_lock:
        entry = live_streams.pop(stream_id, None)
    if entry is None:
        return
    conn = entry.get("conn")
    if conn:
        try: conn.close()
        except Exception: pass
    with live_preview_lock:
        live_previews.pop(stream_id, None)
    if notify:
        broadcast_text(f"ENDED:{stream_id}")
    root.after(0, update_counts)
    root.after(0, lambda: refresh_client_row(stream_id))
    root.after(0, lambda: log(f"Stream ended: {stream_id}"))

def relay_upload(conn):
    receiver  = FrameReceiver()
    stream_id = None
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            receiver.feed(chunk)
            for msg_type, payload in receiver.pop_messages():
                if stream_id is None:
                    if msg_type != TYPE_TEXT:
                        continue
                    text = payload.decode(errors="ignore").strip()
                    if not text.startswith("ID:"):
                        continue
                    candidate = text.split("ID:", 1)[1].strip()
                    with live_state_lock:
                        entry = live_streams.get(candidate)
                        if entry is not None and entry["conn"] is None:
                            entry["conn"] = conn
                            stream_id = candidate
                    if stream_id is None:
                        return
                    continue
                if msg_type == TYPE_LIVE:
                    broadcast(TYPE_LIVE, pack_live_payload(stream_id, payload))
                    np_frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                    if np_frame is not None:
                        rgb = cv2.cvtColor(np_frame, cv2.COLOR_BGR2RGB)
                        with live_preview_lock:
                            live_previews[stream_id] = rgb
    except Exception:
        pass
    finally:
        try: conn.close()
        except Exception: pass
        if stream_id is not None:
            end_stream(stream_id, notify=True)

def accept_uploads():
    while not is_stopped:
        try:
            live_socket.settimeout(1.0)
            conn, addr = live_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        threading.Thread(target=relay_upload, args=(conn,), daemon=True).start()
