import socket
import cv2
import numpy as np
import time
import threading
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext
from datetime import datetime
from PIL import Image, ImageTk
import os

from stream_protocol import (
    TYPE_TEXT, TYPE_VOD, TYPE_LIVE,
    send_message, send_text, FrameReceiver,
    pack_live_payload, unpack_live_payload,
)

# ── VOD state ────────────────────────────────────────────────
# NOTE: there is no longer a single global video_file / is_playing /
# frame_count — each connected client gets its own copy of these
# (see make_client_stream_state / client_info["state"]) so that
# multiple clients can request and watch DIFFERENT videos at the
# same time without interfering with one another.
is_stopped    = False
server_socket = None
video_folder  = ""

# ── Multi-client tracking ───────────────────────────────────
# each entry: {"conn":..., "addr":..., "lock": threading.Lock(), "label": str}
clients      = []
clients_lock = threading.Lock()

# ── Live-broadcast state (clients only — the server never goes live) ─
# Multiple clients can be live AT THE SAME TIME now. Each broadcaster
# gets its own entry keyed by "stream_id" (we reuse its control-socket
# label, e.g. "192.168.1.5:54321", since that's already unique per
# connection). This replaces the old single live_owner/live_upload_conn
# globals, which only ever allowed one broadcaster system-wide.
LIVE_PORT       = 9998
live_streams    = {}   # stream_id -> {"ip": str, "conn": upload_socket|None}
live_state_lock = threading.Lock()
live_server_socket = None

live_previews       = {}   # stream_id -> latest decoded RGB frame (for GUI preview)
live_preview_lock   = threading.Lock()
selected_preview_id = None  # which stream_id the server GUI is currently previewing

HOST = '0.0.0.0'
PORT = 9999

# ════════════════════════════════════════════════════════════
#  GUI SETUP
# ════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("RTSP Video Server")
root.geometry("780x760")
root.minsize(600, 420)
root.configure(bg="#0d1117")
root.resizable(True, True)

FONT_LABEL  = ("Courier New", 10)
FONT_BOLD   = ("Courier New", 10, "bold")
FONT_LOG    = ("Courier New", 9)
FONT_STATUS = ("Courier New", 12, "bold")

BG      = "#0d1117"
PANEL   = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#00ff88"
ACCENT2 = "#0d9488"
RED     = "#ff4444"
YELLOW  = "#ffd700"
WHITE   = "#e6edf3"
MUTED   = "#8b949e"

def styled_frame(parent, **kwargs):
    return tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                    highlightthickness=1, **kwargs)

def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_box.configure(state='normal')
    log_box.insert(tk.END, f"[{timestamp}]  {msg}\n")
    log_box.see(tk.END)
    log_box.configure(state='disabled')

def set_status(text, color=ACCENT):
    status_var.set(text)
    status_label.configure(fg=color)

# ── Per-client monitor table (server-side GUI) ──────────────
# Each connected client gets its OWN row: address, video, playback
# state, and progress — tracked independently rather than one shared
# global status, since every client can be doing something different.
client_rows = {}   # label -> Treeview item id

def _progress_bar_text(count, total, width=12):
    if not total:
        return "—"
    pct = min(1.0, count / total)
    filled = int(pct * width)
    bar = "█" * filled + "─" * (width - filled)
    return f"{bar}  {count}/{total} ({pct * 100:.0f}%)"

def refresh_client_row(label):
    """Refresh (or insert) the monitor-table row for one client. Only
    ever touches that client's own row — every other client's row is
    untouched, so their displayed state can never bleed into this."""
    with clients_lock:
        info = next((c for c in clients if c["label"] == label), None)
    if info is None:
        return
    state = info["state"]
    video_name = os.path.basename(state["video_file"]) if state["video_file"] else "—"
    pb_state   = state["playback_state"]
    progress   = _progress_bar_text(state["frame_count"], state["total_frames"])
    values = (label, video_name, pb_state, progress)
    tag = pb_state.lower()

    if label in client_rows and client_tree.exists(client_rows[label]):
        client_tree.item(client_rows[label], values=values, tags=(tag,))
    else:
        client_rows[label] = client_tree.insert("", tk.END, values=values, tags=(tag,))

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

def update_viewer_count():
    with clients_lock:
        count = len(clients)
    viewers_var.set(f"{count} viewer{'s' if count != 1 else ''} connected")

def update_video_list():
    if not video_folder:
        return
    exts   = ('.mp4', '.avi', '.mkv', '.mov')
    videos = [f for f in os.listdir(video_folder)
              if f.lower().endswith(exts)]
    video_listbox.delete(0, tk.END)
    for v in videos:
        video_listbox.insert(tk.END, f"  {v}")
    log(f"Found {len(videos)} video(s) in folder.")

def list_videos():
    if not video_folder:
        return []
    exts = ('.mp4', '.avi', '.mkv', '.mov')
    return [f for f in os.listdir(video_folder) if f.lower().endswith(exts)]

# ════════════════════════════════════════════════════════════
#  TIMESTAMP OVERLAY
# ════════════════════════════════════════════════════════════
def add_timestamp(frame, tag=None):
    ts   = datetime.now().strftime("%Y-%m-%d  %H:%M:%S")
    text = f"{tag}  {ts}" if tag else ts
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(frame, text, (12, 34), font, 0.65, (0, 0, 0),      4, cv2.LINE_AA)
    cv2.putText(frame, text, (12, 34), font, 0.65, (255, 255, 255), 2, cv2.LINE_AA)
    return frame

# ════════════════════════════════════════════════════════════
#  BROADCAST HELPERS — send a message/frame to every main client
# ════════════════════════════════════════════════════════════
def broadcast_frame(msg_type, payload):
    with clients_lock:
        dead = []
        for c in clients:
            try:
                send_message(c["conn"], msg_type, payload, lock=c["lock"])
            except Exception:
                dead.append(c)
        for c in dead:
            clients.remove(c)
            log(f"Client {c['addr'][0]} dropped.")
    if dead:
        root.after(0, update_viewer_count)
        for c in dead:
            root.after(0, lambda l=c["label"]: remove_client_row(l))

def broadcast_text(text):
    broadcast_frame(TYPE_TEXT, text.encode())

# ════════════════════════════════════════════════════════════
#  VOD STREAMING THREAD — one per connected client, so each client
#  can request and watch a different video independently
# ════════════════════════════════════════════════════════════
def make_client_stream_state():
    return {
        "video_file":   None,
        "is_playing":   False,
        "frame_count":  0,
        "total_frames": 0,
        "should_stop":  False,  # set when this client disconnects
        # Human-facing state shown in the server's per-client monitor
        # table: "IDLE" (no video requested yet), "READY" (video picked
        # but not started), "PLAYING", "PAUSED", "STOPPED".
        "playback_state": "IDLE",
    }

def stream_video_for_client(client_info):
    """Runs for the lifetime of ONE client connection. Sends VOD
    frames only to that client's own socket, so different clients
    can be on different videos (or different points in the same
    video) at the same time without affecting each other."""
    label = client_info["label"]
    state = client_info["state"]

    cap           = None
    current_file  = None
    fps           = 30
    frame_delay   = 1.0 / fps
    next_tick     = time.monotonic()

    while not is_stopped and not state["should_stop"]:
        if not state["is_playing"] or not state["video_file"]:
            time.sleep(0.05)
            next_tick = time.monotonic()
            continue

        if state["video_file"] != current_file:
            if cap is not None:
                cap.release()
            current_file = state["video_file"]
            cap = cv2.VideoCapture(current_file)
            if not cap.isOpened():
                log(f"[{label}] ERROR: Cannot open {os.path.basename(current_file)}")
                state["is_playing"] = False
                state["playback_state"] = "STOPPED"
                root.after(0, lambda l=label: refresh_client_row(l))
                continue
            state["total_frames"] = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
            fps         = cap.get(cv2.CAP_PROP_FPS) or 30
            frame_delay = 1.0 / fps
            state["frame_count"] = 0
            next_tick = time.monotonic()
            log(f"[{label}] Now streaming: {os.path.basename(current_file)}  |  "
                f"{state['total_frames']} frames @ {fps:.1f} FPS")
            root.after(0, lambda l=label: refresh_client_row(l))
            continue

        ret, frame = cap.read()
        if not ret:
            cap.set(cv2.CAP_PROP_POS_FRAMES, 0)
            state["frame_count"] = 0
            continue

        state["frame_count"] += 1
        root.after(0, lambda l=label: refresh_client_row(l))
        frame = add_timestamp(frame)

        _, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
        try:
            send_message(client_info["conn"], TYPE_VOD, buffer.tobytes(),
                         lock=client_info["lock"])
        except Exception:
            break  # this client's socket is gone; its own thread just exits

        next_tick += frame_delay
        now = time.monotonic()
        sleep_time = next_tick - now
        if sleep_time > 0:
            time.sleep(sleep_time)
        elif sleep_time < -frame_delay * 2:
            next_tick = now

    if cap is not None:
        cap.release()
    log(f"[{label}] Streaming thread ended.")

# ════════════════════════════════════════════════════════════
#  LIVE VIEWING — the server can only WATCH clients' broadcasts;
#  it never goes live itself. Any number of clients can be live
#  at once, each tracked separately by stream_id.
# ════════════════════════════════════════════════════════════
def update_live_preview_frame(stream_id, frame_rgb):
    with live_preview_lock:
        live_previews[stream_id] = frame_rgb

def clear_live_preview(stream_id):
    with live_preview_lock:
        live_previews.pop(stream_id, None)

def add_live_stream_to_gui(stream_id):
    global selected_preview_id
    if stream_id not in live_listbox.get(0, tk.END):
        live_listbox.insert(tk.END, stream_id)
    if selected_preview_id is None:
        selected_preview_id = stream_id
    refresh_live_status()

def remove_live_stream_from_gui(stream_id):
    global selected_preview_id
    items = list(live_listbox.get(0, tk.END))
    if stream_id in items:
        live_listbox.delete(items.index(stream_id))
    if selected_preview_id == stream_id:
        remaining = list(live_listbox.get(0, tk.END))
        selected_preview_id = remaining[0] if remaining else None
    refresh_live_status()

def refresh_live_status():
    with live_state_lock:
        n = len(live_streams)
    if n == 0:
        live_status_var.set("Nobody is live")
    else:
        live_status_var.set(f"🔴 {n} client{'s' if n != 1 else ''} live")

def end_live_stream(stream_id, notify=True):
    """Tear down one broadcaster's stream (called on STOPLIVE, on that
    client's upload socket dying, or on that client disconnecting
    entirely). Only affects THIS stream_id — every other ongoing
    broadcast keeps running untouched."""
    with live_state_lock:
        entry = live_streams.pop(stream_id, None)
    if entry is None:
        return
    conn = entry.get("conn")
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    clear_live_preview(stream_id)
    if notify:
        broadcast_text(f"LIVE_ENDED:{stream_id}")
    root.after(0, lambda: remove_live_stream_from_gui(stream_id))
    root.after(0, lambda: log(f"Live broadcast ended: {stream_id}"))

# ════════════════════════════════════════════════════════════
#  LIVE RELAY — receiving a remote client's camera and
#  rebroadcasting it (tagged with its stream_id) to every other
#  connected client. One thread per broadcaster; independent of
#  every other broadcaster's thread.
# ════════════════════════════════════════════════════════════
def relay_live_stream(conn):
    """One thread per broadcaster. The FIRST message on this fresh
    upload socket must be a TYPE_TEXT "STREAMID:<id>" telling us which
    pending GOLIVE grant this connection belongs to — we match purely
    on this token (not on source IP), so it works correctly even when
    several clients broadcast from behind the same NAT IP. Everything
    after that handshake is treated as LIVE frames. We read handshake
    and frames from the SAME loop (rather than two separate recv
    loops) so a frame that happens to arrive in the same TCP read as
    the handshake text is never silently dropped."""
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
                        continue  # ignore anything before handshake completes
                    text = payload.decode(errors="ignore").strip()
                    if not text.startswith("STREAMID:"):
                        continue
                    candidate = text.split("STREAMID:", 1)[1].strip()
                    with live_state_lock:
                        entry = live_streams.get(candidate)
                        if entry is not None and entry["conn"] is None:
                            entry["conn"] = conn
                            stream_id = candidate
                    if stream_id is None:
                        return  # no matching / already-claimed grant — reject
                    log(f"Live upload connected: stream {stream_id}")
                    continue

                if msg_type == TYPE_LIVE:
                    wrapped = pack_live_payload(stream_id, payload)
                    broadcast_frame(TYPE_LIVE, wrapped)
                    np_frame = cv2.imdecode(
                        np.frombuffer(payload, dtype=np.uint8),
                        cv2.IMREAD_COLOR)
                    if np_frame is not None:
                        rgb = cv2.cvtColor(np_frame, cv2.COLOR_BGR2RGB)
                        root.after(0, lambda sid=stream_id, f=rgb:
                                   update_live_preview_frame(sid, f))
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
        if stream_id is not None:
            end_live_stream(stream_id, notify=True)

def accept_live_uploads():
    log(f"Live-relay listening on port {LIVE_PORT}...")
    while not is_stopped:
        try:
            live_server_socket.settimeout(1.0)
            conn, addr = live_server_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            break
        # No IP-based gatekeeping here anymore — relay_live_stream()
        # authenticates the connection itself via the STREAMID
        # handshake, which lets any number of clients (even sharing an
        # IP) each have their own broadcast in flight simultaneously.
        threading.Thread(target=relay_live_stream, args=(conn,), daemon=True).start()

# ════════════════════════════════════════════════════════════
#  PER-CLIENT COMMAND LISTENER
# ════════════════════════════════════════════════════════════
def listen_for_commands(conn, addr):
    global is_stopped

    label = f"{addr[0]}:{addr[1]}"
    client_info = {
        "conn": conn, "addr": addr, "lock": threading.Lock(), "label": label,
        "state": make_client_stream_state(),
    }
    with clients_lock:
        clients.append(client_info)
    root.after(0, update_viewer_count)
    root.after(0, lambda l=label: refresh_client_row(l))
    log(f"✓ Client connected: {label}")
    root.after(0, lambda: set_status("✓ CLIENT CONNECTED", ACCENT2))

    # This client gets its own independent streaming thread, so it can
    # be watching a completely different video than every other client.
    threading.Thread(target=stream_video_for_client, args=(client_info,),
                      daemon=True).start()

    # Send the video list right away
    videos = list_videos()
    try:
        send_text(conn, f"VIDEOLIST:{','.join(videos)}", lock=client_info["lock"])
        log(f"[{label}] Sent video list ({len(videos)} videos)")
    except Exception as e:
        log(f"[{label}] Failed to send video list: {e}")

    # Let the new client know about every broadcast already in progress
    # (there can be several at once now — tell them about all of them).
    with live_state_lock:
        active_ids = list(live_streams.keys())
    for sid in active_ids:
        try:
            send_text(conn, f"LIVE_STARTED:{sid}", lock=client_info["lock"])
        except Exception:
            pass

    receiver = FrameReceiver()
    while not is_stopped:
        try:
            chunk = conn.recv(4096)
            if not chunk:
                raise ConnectionError("Client closed connection.")
            receiver.feed(chunk)
            for msg_type, payload in receiver.pop_messages():
                if msg_type != TYPE_TEXT:
                    continue  # clients only ever send text commands here
                cmd = payload.decode(errors="ignore").strip()
                if not cmd:
                    continue
                log(f"[{label}] ← {cmd.splitlines()[0]}")

                if cmd.startswith("REQUEST:"):
                    requested = cmd.split("REQUEST:")[1].strip()
                    full_path = os.path.join(video_folder, requested)
                    if os.path.exists(full_path):
                        # Scoped to THIS client only — other clients' videos
                        # are untouched, so everyone can watch something
                        # different at the same time.
                        client_info["state"]["video_file"] = full_path
                        client_info["state"]["playback_state"] = "READY"
                        client_info["state"]["frame_count"] = 0
                        client_info["state"]["total_frames"] = 0
                        send_text(conn, f"RTSP/1.0 200 OK\r\nVideo: {requested}\r\n\r\n",
                                  lock=client_info["lock"])
                        log(f"[{label}] Video set → {requested}")
                        root.after(0, lambda l=label: refresh_client_row(l))
                    else:
                        send_text(conn, "RTSP/1.0 404 Not Found\r\n\r\n", lock=client_info["lock"])
                        log(f"[{label}] NOT FOUND: {requested}")

                elif "PLAY" in cmd and cmd.startswith("PLAY"):
                    if not client_info["state"]["video_file"]:
                        send_text(conn,
                                  "RTSP/1.0 400 Bad Request\r\nReason: No video selected\r\n\r\n",
                                  lock=client_info["lock"])
                        log(f"[{label}] PLAY rejected — no video selected")
                        continue
                    client_info["state"]["is_playing"] = True
                    client_info["state"]["playback_state"] = "PLAYING"
                    send_text(conn, "RTSP/1.0 200 OK\r\nState: PLAYING\r\n\r\n", lock=client_info["lock"])
                    root.after(0, lambda l=label: refresh_client_row(l))
                    log(f"[{label}] State → PLAYING")

                elif cmd.startswith("PAUSE"):
                    client_info["state"]["is_playing"] = False
                    client_info["state"]["playback_state"] = "PAUSED"
                    send_text(conn, "RTSP/1.0 200 OK\r\nState: PAUSED\r\n\r\n", lock=client_info["lock"])
                    root.after(0, lambda l=label: refresh_client_row(l))
                    log(f"[{label}] State → PAUSED")

                elif cmd.startswith("STOP"):
                    client_info["state"]["is_playing"] = False
                    client_info["state"]["playback_state"] = "STOPPED"
                    send_text(conn, "RTSP/1.0 200 OK\r\nState: STOPPED\r\n\r\n", lock=client_info["lock"])
                    root.after(0, lambda l=label: refresh_client_row(l))
                    log(f"[{label}] State → STOPPED")

                elif cmd.startswith("GOLIVE"):
                    # Every client is free to go live regardless of how many
                    # OTHER clients are already broadcasting — we just hand
                    # out a fresh stream_id (this client's own label is
                    # already unique) and register a pending slot for it.
                    stream_id = label
                    with live_state_lock:
                        live_streams[stream_id] = {"ip": addr[0], "conn": None}
                    send_text(conn,
                              f"RTSP/1.0 200 OK\r\nLive: GRANTED\r\nStreamID: {stream_id}\r\n\r\n",
                              lock=client_info["lock"])
                    log(f"[{label}] Granted live broadcast (stream {stream_id}).")
                    broadcast_text(f"LIVE_STARTED:{stream_id}")
                    root.after(0, lambda sid=stream_id: add_live_stream_to_gui(sid))

                elif cmd.startswith("STOPLIVE"):
                    stream_id = label
                    with live_state_lock:
                        is_owner = stream_id in live_streams
                    if is_owner:
                        send_text(conn, "RTSP/1.0 200 OK\r\nLive: STOPPED\r\n\r\n",
                                  lock=client_info["lock"])
                        end_live_stream(stream_id, notify=True)
                        log(f"[{label}] Ended live broadcast.")

        except Exception as e:
            log(f"[{label}] Error: {e}")
            break

    client_info["state"]["should_stop"] = True  # let its streaming thread exit

    with clients_lock:
        clients[:] = [c for c in clients if c["conn"] is not conn]
    root.after(0, update_viewer_count)
    root.after(0, lambda l=label: remove_client_row(l))

    # If this client was live and dropped without saying STOPLIVE, clean up
    # ONLY its own stream — every other broadcaster keeps streaming.
    with live_state_lock:
        was_live = label in live_streams
    if was_live:
        end_live_stream(label, notify=True)

    log(f"[{label}] Disconnected.")
    try:
        conn.close()
    except Exception:
        pass

# ════════════════════════════════════════════════════════════
#  ACCEPT LOOP — MULTIPLE CLIENTS
# ════════════════════════════════════════════════════════════
def accept_clients():
    global is_stopped
    log(f"Server listening on port {PORT}...")
    set_status("⏳ Waiting for client...", YELLOW)

    while not is_stopped:
        try:
            server_socket.settimeout(1.0)
            conn, addr = server_socket.accept()
        except socket.timeout:
            continue
        except Exception:
            break

        threading.Thread(target=listen_for_commands, args=(conn, addr), daemon=True).start()

# ════════════════════════════════════════════════════════════
#  LIVE PREVIEW DISPLAY LOOP (server-side canvas)
# ════════════════════════════════════════════════════════════
def update_live_canvas():
    with live_preview_lock:
        frame = live_previews.get(selected_preview_id) if selected_preview_id else None
    w = live_canvas.winfo_width()  or 320
    h = live_canvas.winfo_height() or 180
    if frame is not None:
        try:
            img   = Image.fromarray(frame).resize((w, h), Image.LANCZOS)
            photo = ImageTk.PhotoImage(img)
            live_canvas.photo = photo
            live_canvas.delete("all")
            live_canvas.create_image(0, 0, anchor='nw', image=photo)
        except Exception:
            pass
    else:
        live_canvas.delete("all")
        live_canvas.create_rectangle(0, 0, w, h, fill="#0a0f14")
        msg = f"Waiting for {selected_preview_id}..." if selected_preview_id else "No live broadcast"
        live_canvas.create_text(w // 2, h // 2, text=msg,
                                 fill=MUTED, font=("Courier New", 10))
    root.after(40, update_live_canvas)

# ════════════════════════════════════════════════════════════
#  SERVER CONTROLS
# ════════════════════════════════════════════════════════════
def choose_folder():
    global video_folder
    folder = filedialog.askdirectory(title="Select Video Folder")
    if folder:
        video_folder = folder
        folder_var.set(os.path.basename(folder))
        log(f"Video folder: {folder}")
        update_video_list()
        # If the server is already running, push the updated list to
        # everyone who's connected instead of waiting for a reconnect.
        if server_socket is not None and not is_stopped:
            videos = list_videos()
            broadcast_text(f"VIDEOLIST:{','.join(videos)}")
            log(f"Broadcasted updated video list to connected clients.")

def start_server():
    global server_socket, is_stopped, live_server_socket

    is_stopped = False

    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server_socket.bind((HOST, PORT))
    server_socket.listen(5)

    live_server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    live_server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    live_server_socket.bind((HOST, LIVE_PORT))
    live_server_socket.listen(5)

    try:
        local_ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        local_ip = "127.0.0.1"

    log(f"Server IP: {local_ip}  |  Control/VOD port: {PORT}  |  Live port: {LIVE_PORT}")
    log("Share the IP with clients on the same network.")
    if not video_folder:
        log("No video folder selected yet — clients can connect now; "
            "pick a folder anytime and they'll get the updated list.")
    start_btn.configure(state='disabled')
    stop_btn.configure(state='normal', bg=RED)

    threading.Thread(target=accept_clients,      daemon=True).start()
    threading.Thread(target=accept_live_uploads, daemon=True).start()

def stop_server():
    global is_stopped
    is_stopped = True
    set_status("■  STOPPED", RED)
    live_status_var.set("Nobody is live")
    log("Server stopped.")
    with clients_lock:
        for c in clients:
            try:
                c["conn"].close()
            except Exception:
                pass
        clients.clear()
    clear_client_rows()
    with live_state_lock:
        for entry in live_streams.values():
            conn = entry.get("conn")
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        live_streams.clear()
    with live_preview_lock:
        live_previews.clear()
    if server_socket:
        try:
            server_socket.close()
        except Exception:
            pass
    if live_server_socket:
        try:
            live_server_socket.close()
        except Exception:
            pass
    update_viewer_count()
    start_btn.configure(state='normal')
    stop_btn.configure(state='disabled', bg=MUTED)

# ════════════════════════════════════════════════════════════
#  GUI LAYOUT
# ════════════════════════════════════════════════════════════

# ── Bottom button bar ────────────────────────────────────────
# Packed FIRST with side='bottom' so it reserves its space at the
# bottom of the window and is ALWAYS visible, no matter how tall the
# rest of the content is or how small the window gets resized.
btn_frame = tk.Frame(root, bg=BG)
btn_frame.pack(side='bottom', fill='x', padx=20, pady=(6, 18))
start_btn = tk.Button(btn_frame, text="▶  START SERVER", font=FONT_BOLD,
    bg=ACCENT, fg="#0d1117", relief='flat', cursor='hand2',
    padx=24, pady=10, command=lambda: start_server())
start_btn.pack(side='left', padx=(0, 10))
stop_btn = tk.Button(btn_frame, text="■  STOP SERVER", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=24, pady=10, state='disabled', command=lambda: stop_server())
stop_btn.pack(side='left')
tk.Button(btn_frame, text="QUIT", font=FONT_BOLD,
    bg=PANEL, fg=MUTED, relief='flat', cursor='hand2',
    padx=24, pady=10, command=root.quit).pack(side='right')

tk.Frame(root, bg=BORDER, height=1).pack(side='bottom', fill='x')

# ── Scrollable content area ──────────────────────────────────
# Everything above the button bar lives inside a canvas+scrollbar so
# that on small/short windows the content scrolls instead of pushing
# the START/STOP buttons off the bottom of the screen.
scroll_container = tk.Frame(root, bg=BG)
scroll_container.pack(side='top', fill='both', expand=True)

main_canvas = tk.Canvas(scroll_container, bg=BG, highlightthickness=0)
main_scrollbar = ttk.Scrollbar(scroll_container, orient='vertical',
                                command=main_canvas.yview)
scroll_frame = tk.Frame(main_canvas, bg=BG)

scroll_frame.bind(
    "<Configure>",
    lambda e: main_canvas.configure(scrollregion=main_canvas.bbox("all"))
)
canvas_window = main_canvas.create_window((0, 0), window=scroll_frame, anchor='nw')
main_canvas.configure(yscrollcommand=main_scrollbar.set)

def _resize_scroll_frame(event):
    # keep the inner frame the same width as the visible canvas so
    # fill='x' children stretch correctly instead of clipping/shrinking
    main_canvas.itemconfig(canvas_window, width=event.width)

main_canvas.bind("<Configure>", _resize_scroll_frame)
main_canvas.pack(side='left', fill='both', expand=True)
main_scrollbar.pack(side='right', fill='y')

def _on_mousewheel(event):
    if event.num == 5 or event.delta < 0:
        main_canvas.yview_scroll(1, "units")
    elif event.num == 4 or event.delta > 0:
        main_canvas.yview_scroll(-1, "units")

main_canvas.bind_all("<MouseWheel>", _on_mousewheel)   # Windows / macOS
main_canvas.bind_all("<Button-4>", _on_mousewheel)     # Linux scroll up
main_canvas.bind_all("<Button-5>", _on_mousewheel)     # Linux scroll down

title_frame = tk.Frame(scroll_frame, bg=BG)
title_frame.pack(fill='x', padx=20, pady=(18, 0))
tk.Label(title_frame, text="◈ RTSP", font=("Courier New", 22, "bold"),
         bg=BG, fg=ACCENT).pack(side='left')
tk.Label(title_frame, text=" VIDEO SERVER", font=("Courier New", 22, "bold"),
         bg=BG, fg=WHITE).pack(side='left')
tk.Label(title_frame, text=f"PORT {PORT} / LIVE {LIVE_PORT}", font=FONT_LABEL,
         bg=BG, fg=MUTED).pack(side='right', pady=6)
tk.Frame(scroll_frame, bg=BORDER, height=1).pack(fill='x', padx=20, pady=10)

# ── Folder Selection ─────────────────────────────────────────
file_frame = styled_frame(scroll_frame)
file_frame.pack(fill='x', padx=20, pady=6)
tk.Label(file_frame, text="  VIDEO FOLDER", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 2))
row = tk.Frame(file_frame, bg=PANEL)
row.pack(fill='x', padx=12, pady=(0, 10))
folder_var = tk.StringVar(value="No folder selected")
tk.Label(row, textvariable=folder_var, font=FONT_LABEL,
         bg="#0d1117", fg=WHITE, anchor='w', width=48,
         padx=8, pady=6, relief='flat').pack(side='left')
tk.Button(row, text="BROWSE FOLDER", font=FONT_BOLD, bg=ACCENT2, fg=WHITE,
          relief='flat', cursor='hand2', padx=12, pady=4,
          command=choose_folder).pack(side='left', padx=(8, 0))

# ── Video List ───────────────────────────────────────────────
vlist_frame = styled_frame(scroll_frame)
vlist_frame.pack(fill='x', padx=20, pady=6)
tk.Label(vlist_frame, text="  VIDEOS IN FOLDER", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
video_listbox = tk.Listbox(vlist_frame, font=FONT_LOG,
    bg="#0d1117", fg=ACCENT, relief='flat', height=4,
    selectbackground=ACCENT2, highlightthickness=0, borderwidth=0)
video_listbox.pack(fill='x', padx=12, pady=(0, 10))

# ── Server Info ──────────────────────────────────────────────
info_frame = styled_frame(scroll_frame)
info_frame.pack(fill='x', padx=20, pady=6)
tk.Label(info_frame, text="  SERVER INFO", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
info_row = tk.Frame(info_frame, bg=PANEL)
info_row.pack(fill='x', padx=12, pady=(0, 10))

status_var      = tk.StringVar(value="■  IDLE")
viewers_var     = tk.StringVar(value="0 viewers connected")

def info_col(parent, label, var, color=WHITE):
    f = tk.Frame(parent, bg=PANEL)
    f.pack(side='left', padx=(0, 30))
    tk.Label(f, text=label, font=FONT_LABEL, bg=PANEL, fg=MUTED).pack(anchor='w')
    tk.Label(f, textvariable=var, font=FONT_BOLD, bg=PANEL, fg=color).pack(anchor='w')

f2 = tk.Frame(info_row, bg=PANEL)
f2.pack(side='left', padx=(0, 30))
tk.Label(f2, text="STATUS", font=FONT_LABEL, bg=PANEL, fg=MUTED).pack(anchor='w')
status_label = tk.Label(f2, textvariable=status_var, font=FONT_STATUS,
                        bg=PANEL, fg=MUTED)
status_label.pack(anchor='w')
info_col(info_row, "VIEWERS", viewers_var, ACCENT)

# ── Per-Client Monitor ───────────────────────────────────────
# Every connected client gets its own row here — address, video,
# playback state, and progress — tracked and refreshed independently,
# instead of one shared global "now playing" summary.
mon_frame = styled_frame(scroll_frame)
mon_frame.pack(fill='both', expand=True, padx=20, pady=6)
tk.Label(mon_frame, text="  CLIENT MONITOR", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
mon_row = tk.Frame(mon_frame, bg=PANEL)
mon_row.pack(fill='both', expand=True, padx=12, pady=(0, 10))

style = ttk.Style()
style.theme_use('clam')
style.configure("Client.Treeview",
                background="#0d1117", fieldbackground="#0d1117",
                foreground=WHITE, rowheight=24, borderwidth=0,
                font=FONT_LOG)
style.configure("Client.Treeview.Heading",
                background=PANEL, foreground=MUTED, font=FONT_BOLD,
                borderwidth=0)
style.map("Client.Treeview", background=[("selected", ACCENT2)])

client_tree = ttk.Treeview(mon_row, style="Client.Treeview",
    columns=("client", "video", "state", "progress"),
    show="headings", height=6, selectmode="none")
client_tree.heading("client",   text="CLIENT")
client_tree.heading("video",    text="VIDEO")
client_tree.heading("state",   text="STATE")
client_tree.heading("progress", text="PROGRESS")
client_tree.column("client",   width=140, anchor='w')
client_tree.column("video",    width=170, anchor='w')
client_tree.column("state",    width=90,  anchor='center')
client_tree.column("progress", width=230, anchor='w')
client_tree.tag_configure("playing", foreground=ACCENT)
client_tree.tag_configure("paused",  foreground=YELLOW)
client_tree.tag_configure("stopped", foreground=RED)
client_tree.tag_configure("ready",   foreground=WHITE)
client_tree.tag_configure("idle",    foreground=MUTED)
client_tree.pack(side='left', fill='both', expand=True)

mon_scroll = ttk.Scrollbar(mon_row, orient='vertical', command=client_tree.yview)
client_tree.configure(yscrollcommand=mon_scroll.set)
mon_scroll.pack(side='right', fill='y')

# ── Live Viewing Panel (server can only watch, never go live) ──
# Any number of clients can be live at once, so this shows a pick-list
# of everyone currently broadcasting; click a name to preview it.
live_frame = styled_frame(scroll_frame)
live_frame.pack(fill='x', padx=20, pady=6)
tk.Label(live_frame, text="  LIVE VIEW", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
live_row = tk.Frame(live_frame, bg=PANEL)
live_row.pack(fill='x', padx=12, pady=(0, 10))

live_canvas = tk.Canvas(live_row, width=280, height=158,
                         bg="#0a0f14", highlightthickness=1,
                         highlightbackground=BORDER)
live_canvas.pack(side='left', padx=(0, 12))

live_side = tk.Frame(live_row, bg=PANEL)
live_side.pack(side='left', fill='both', expand=True)
live_status_var = tk.StringVar(value="Nobody is live")
tk.Label(live_side, textvariable=live_status_var, font=FONT_BOLD,
         bg=PANEL, fg=RED).pack(anchor='w', pady=(4, 4))
tk.Label(live_side, text="Multiple clients can be live at once.\n"
                         "Select one below to preview it here.\n"
                         "The server itself can't go live.",
         font=("Courier New", 8), bg=PANEL, fg=MUTED, justify='left'
         ).pack(anchor='w', pady=(0, 6))
live_listbox = tk.Listbox(live_side, font=FONT_LOG, bg="#0d1117", fg=ACCENT,
    relief='flat', height=3, selectbackground=ACCENT2,
    highlightthickness=0, borderwidth=0)
live_listbox.pack(fill='x')

def on_live_select(event=None):
    global selected_preview_id
    sel = live_listbox.curselection()
    if sel:
        selected_preview_id = live_listbox.get(sel[0])

live_listbox.bind("<<ListboxSelect>>", on_live_select)

# ── Log ───────────────────────────────────────────────────────
log_frame = styled_frame(scroll_frame)
log_frame.pack(fill='both', expand=True, padx=20, pady=6)
tk.Label(log_frame, text="  SERVER LOG", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
log_box = scrolledtext.ScrolledText(log_frame, font=FONT_LOG,
    bg="#0d1117", fg=ACCENT, insertbackground=ACCENT,
    relief='flat', height=7, state='disabled',
    selectbackground=ACCENT2)
log_box.pack(fill='both', expand=True, padx=12, pady=(0, 12))

log("Server GUI ready. Click START SERVER anytime — pick a video folder now or later.")
update_live_canvas()
root.mainloop()
