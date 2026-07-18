import socket
import cv2
import numpy as np
import threading
import tkinter as tk
from tkinter import scrolledtext, ttk
from PIL import Image, ImageTk
from datetime import datetime
import time

from stream_protocol import (
    TYPE_TEXT, TYPE_VOD, TYPE_LIVE,
    send_message, send_text, FrameReceiver,
    pack_live_payload, unpack_live_payload,
)

# ── VOD / connection state ──────────────────────────────────
current_state    = "IDLE"     # IDLE, CONNECTED, REQUESTED, PLAY, PAUSE, STOP
vod_frame_buffer = None
buffer_lock      = threading.Lock()
client_socket    = None
running          = False
reconnecting     = False
server_ip        = "127.0.0.1"
is_fullscreen    = False
available_videos = []
current_video_index = -1   # index into available_videos, for NEXT / PREVIOUS

# ── View mode — which feed the canvas is currently showing ──
# "VOD"         -> normal video-on-demand playback
# "OWN_LIVE"    -> this client's own outgoing broadcast (local preview)
# "WATCH_LIVE"  -> watching another client's broadcast (watching_stream_id)
view_mode = "VOD"

# ── Live-broadcast state ────────────────────────────────────
# Any number of clients (including this one) can be live at the same
# time now. Every OTHER client's broadcast we know about lives in
# `live_streams`, keyed by its stream_id (assigned by the server).
LIVE_PORT           = 9998
live_streams        = {}      # stream_id -> latest decoded RGB frame (or None)
live_streams_lock   = threading.Lock()
watching_stream_id  = None     # which entry in live_streams we're currently viewing

own_stream_id       = None     # stream_id the server assigned to OUR broadcast
is_broadcasting     = False    # is THIS client the one going live
own_cam             = None
live_send_socket    = None
local_preview_buf   = None     # own camera preview while broadcasting (no round trip)
awaiting_live_grant = False

PORT            = 9999
RECONNECT_DELAY = 5
MAX_RECONNECTS  = 10

# ════════════════════════════════════════════════════════════
#  GUI SETUP
# ════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("RTSP Video Client")
root.geometry("900x780")
root.configure(bg="#0d1117")
root.resizable(True, True)

FONT_LABEL  = ("Courier New", 10)
FONT_BOLD   = ("Courier New", 10, "bold")
FONT_LOG    = ("Courier New", 9)
FONT_STATUS = ("Courier New", 11, "bold")

BG      = "#0d1117"
PANEL   = "#161b22"
BORDER  = "#30363d"
ACCENT  = "#00ff88"
ACCENT2 = "#0d9488"
RED     = "#ff4444"
YELLOW  = "#ffd700"
BLUE    = "#58a6ff"
WHITE   = "#e6edf3"
MUTED   = "#8b949e"

# ════════════════════════════════════════════════════════════
#  HELPERS
# ════════════════════════════════════════════════════════════
def log(msg):
    timestamp = datetime.now().strftime("%H:%M:%S")
    log_box.configure(state='normal')
    log_box.insert(tk.END, f"[{timestamp}]  {msg}\n")
    log_box.see(tk.END)
    log_box.configure(state='disabled')

def set_status(text, color=ACCENT):
    status_var.set(text)
    status_label.configure(fg=color)

def styled_frame(parent, **kwargs):
    return tk.Frame(parent, bg=PANEL, highlightbackground=BORDER,
                    highlightthickness=1, **kwargs)

def send_cmd(text):
    """Send a text control message upstream to the server."""
    if client_socket is None:
        log("Not connected to server.")
        return
    try:
        send_text(client_socket, text)
    except Exception as e:
        log(f"Send error: {e}")

# ════════════════════════════════════════════════════════════
#  BUTTON STATE MANAGER
# ════════════════════════════════════════════════════════════
def update_buttons():
    if current_state == "PLAY":
        play_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        pause_btn.configure(state='normal',  bg=YELLOW,  fg="#0d1117")
        stop_btn.configure(state='normal',   bg=RED,     fg=WHITE)
        fs_btn.configure(state='normal',     bg=ACCENT2, fg=WHITE)
        request_btn.configure(state='disabled', bg=MUTED, fg=WHITE)
    elif current_state == "PAUSE":
        play_btn.configure(state='normal',   bg=ACCENT,  fg="#0d1117")
        pause_btn.configure(state='disabled',bg=MUTED,   fg=WHITE)
        stop_btn.configure(state='normal',   bg=RED,     fg=WHITE)
        fs_btn.configure(state='normal',     bg=ACCENT2, fg=WHITE)
        request_btn.configure(state='disabled', bg=MUTED, fg=WHITE)
    elif current_state == "CONNECTED":
        play_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        pause_btn.configure(state='disabled',bg=MUTED,   fg=WHITE)
        stop_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        fs_btn.configure(state='disabled',   bg=MUTED,   fg=WHITE)
        request_btn.configure(state='normal', bg=ACCENT2, fg=WHITE)
    elif current_state == "REQUESTED":
        play_btn.configure(state='normal',   bg=ACCENT,  fg="#0d1117")
        pause_btn.configure(state='disabled',bg=MUTED,   fg=WHITE)
        stop_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        fs_btn.configure(state='disabled',   bg=MUTED,   fg=WHITE)
        request_btn.configure(state='normal', bg=ACCENT2, fg=WHITE)
    elif current_state == "STOP":
        play_btn.configure(state='normal',   bg=ACCENT,  fg="#0d1117")
        pause_btn.configure(state='disabled',bg=MUTED,   fg=WHITE)
        stop_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        fs_btn.configure(state='disabled',   bg=MUTED,   fg=WHITE)
        request_btn.configure(state='normal', bg=ACCENT2, fg=WHITE)
    else:  # IDLE
        play_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        pause_btn.configure(state='disabled',bg=MUTED,   fg=WHITE)
        stop_btn.configure(state='disabled', bg=MUTED,   fg=WHITE)
        fs_btn.configure(state='disabled',   bg=MUTED,   fg=WHITE)
        request_btn.configure(state='disabled', bg=MUTED, fg=WHITE)

    # Live-related buttons depend on connection state; going live no
    # longer depends on whether anyone else is already broadcasting —
    # every client can have its own simultaneous live stream.
    connected = client_socket is not None and running
    if is_broadcasting:
        live_btn.configure(text="■  END LIVE", bg=RED, state='normal' if connected else 'disabled')
    else:
        live_btn.configure(text="🔴  GO LIVE", bg=ACCENT2 if connected else MUTED,
                            state='normal' if connected else 'disabled')

    with live_streams_lock:
        others = sorted(live_streams.keys())
    have_others = len(others) > 0
    watch_btn.configure(state='normal' if (have_others and connected) else 'disabled',
                         bg=ACCENT2 if (have_others and connected) else MUTED)
    stop_watch_btn.configure(state='normal' if view_mode == "WATCH_LIVE" else 'disabled',
                              bg=RED if view_mode == "WATCH_LIVE" else MUTED)

    have_videos = len(available_videos) > 0
    nav_state = 'normal' if (connected and have_videos) else 'disabled'
    prev_btn.configure(state=nav_state, bg=ACCENT2 if nav_state == 'normal' else MUTED)
    next_btn.configure(state=nav_state, bg=ACCENT2 if nav_state == 'normal' else MUTED)

# ════════════════════════════════════════════════════════════
#  VIDEO SELECTION — CLIENT REQUESTS A VIDEO FROM SERVER
# ════════════════════════════════════════════════════════════
def cut_to_vod():
    """Cut away from whatever live feed is currently on screen —
    someone else's broadcast, or our own outgoing preview — and switch
    the canvas back to video-on-demand. Called whenever the user acts
    on VOD controls (REQUEST, PLAY, NEXT, PREVIOUS) so picking a video
    always wins over whatever live view was showing. Doesn't stop an
    outgoing broadcast, just what this client is looking at."""
    global view_mode, watching_stream_id
    if view_mode == "WATCH_LIVE":
        log(f"Stopping live view of {watching_stream_id} — switching to video on demand.")
        watching_stream_id = None
        view_mode = "VOD"
    elif view_mode == "OWN_LIVE":
        log("Switching view to video on demand (your broadcast keeps running).")
        view_mode = "VOD"
    update_buttons()

def request_video(autoplay=False):
    global current_state, current_video_index
    selected = video_var.get()
    if not selected or selected == "── Select a video ──":
        log("⚠  Please select a video first.")
        return
    if selected in available_videos:
        current_video_index = available_videos.index(selected)
    cut_to_vod()
    send_cmd(f"REQUEST:{selected}")
    log(f"Requested: {selected}")
    now_playing_var.set(f"▶  {selected}")
    current_state = "REQUESTED"
    update_buttons()
    if autoplay:
        do_play()

def populate_video_dropdown(video_list):
    global available_videos, current_video_index
    available_videos = video_list
    video_dropdown['values'] = video_list
    if video_list:
        # Keep pointing at the same video if it's still in the (possibly
        # updated) list; otherwise reset to the first one.
        if 0 <= current_video_index < len(video_list):
            video_dropdown.current(current_video_index)
        else:
            video_dropdown.current(0)
            current_video_index = 0
        log(f"Available videos: {', '.join(video_list)}")
    else:
        current_video_index = -1
        log("No videos found on server.")
    update_buttons()

def select_video(index, announce=True):
    """Move the dropdown selection to the video at `index` in the list.
    Just changes what's selected in the dropdown — doesn't request or
    play anything by itself."""
    global current_video_index
    if not available_videos:
        log("⚠  No videos available yet.")
        return
    index = index % len(available_videos)
    current_video_index = index
    selected = available_videos[index]

    video_var.set(selected)
    video_dropdown.current(index)
    if announce:
        log(f"Selected: {selected}  (click REQUEST VIDEO or PLAY to start it)")

def do_next():
    """Advance to the next video in the list and start playing it
    right away — no extra click needed."""
    if not available_videos:
        log("⚠  No videos available yet.")
        return
    start = current_video_index if current_video_index >= 0 else -1
    select_video(start + 1, announce=False)
    request_video(autoplay=True)

def do_previous():
    """Go back to the previous video in the list and start playing it
    right away — no extra click needed."""
    if not available_videos:
        log("⚠  No videos available yet.")
        return
    start = current_video_index if current_video_index >= 0 else 1
    select_video(start - 1, announce=False)
    request_video(autoplay=True)

# ════════════════════════════════════════════════════════════
#  RTSP COMMAND SENDER (VOD playback controls)
# ════════════════════════════════════════════════════════════
def send_command(cmd_type):
    global current_state
    msg = f"{cmd_type} rtsp://server/video RTSP/1.0\r\nCSeq: 1\r\nSession: 12345\r\n\r\n"
    send_cmd(msg)
    current_state = cmd_type
    log(f"Sent: {cmd_type}")
    update_buttons()

# ════════════════════════════════════════════════════════════
#  LIVE BROADCASTING (this client goes live)
# ════════════════════════════════════════════════════════════
def toggle_own_live():
    global awaiting_live_grant
    if is_broadcasting:
        stop_own_live()
        return
    # Every client is free to go live even if others already are —
    # no check against other broadcasters here anymore.
    awaiting_live_grant = True
    log("Requesting permission to go live...")
    send_cmd("GOLIVE\r\n")

def begin_own_live():
    """Called once the server has granted the live request and told us
    our stream_id."""
    global is_broadcasting, own_cam, live_send_socket, view_mode
    if not own_stream_id:
        log("Live grant received but no stream id — aborting.")
        return
    own_cam = cv2.VideoCapture(0)
    if not own_cam.isOpened():
        log("No camera found on this machine.")
        send_cmd("STOPLIVE\r\n")
        return
    try:
        live_send_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        live_send_socket.connect((server_ip, LIVE_PORT))
        # Handshake so the server knows which pending grant this raw
        # upload socket belongs to (it never sends framed video back
        # to us on this socket, only reads from it).
        send_text(live_send_socket, f"STREAMID:{own_stream_id}")
    except Exception as e:
        log(f"Could not open live-upload connection: {e}")
        own_cam.release()
        own_cam = None
        send_cmd("STOPLIVE\r\n")
        return

    is_broadcasting = True
    view_mode = "OWN_LIVE"
    set_status("🔴 LIVE", RED)
    log(f"You are now live (stream id {own_stream_id}).")
    update_buttons()
    threading.Thread(target=camera_capture_loop, daemon=True).start()

def camera_capture_loop():
    global local_preview_buf, is_broadcasting
    delay = 1 / 20
    while is_broadcasting:
        ret, frame = own_cam.read()
        if not ret:
            time.sleep(0.05)
            continue
        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        # Mirror ONLY what this client sees of itself — like Zoom, Teams,
        # and TikTok Live do — so it feels natural (moving your right
        # hand appears to move on your right side of the preview). The
        # frame actually sent to other viewers below is left unflipped,
        # so everyone else sees you the way a camera facing you would.
        with buffer_lock:
            local_preview_buf = cv2.flip(frame_rgb, 1)
        _, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
        try:
            send_message(live_send_socket, TYPE_LIVE, buf.tobytes())
        except Exception as e:
            root.after(0, lambda: log(f"Live send error: {e}"))
            break
        time.sleep(delay)

    try:
        own_cam.release()
    except Exception:
        pass
    try:
        live_send_socket.close()
    except Exception:
        pass

def stop_own_live():
    global is_broadcasting, view_mode, local_preview_buf, own_stream_id
    is_broadcasting = False
    if view_mode == "OWN_LIVE":
        view_mode = "VOD"
    local_preview_buf = None
    send_cmd("STOPLIVE\r\n")
    own_stream_id = None
    set_status("CONNECTED", ACCENT2)
    log("You ended your live broadcast.")
    update_buttons()

# ════════════════════════════════════════════════════════════
#  WATCH SOMEONE ELSE'S LIVE STREAM
# ════════════════════════════════════════════════════════════
# Any number of OTHER clients can be live at once; this client picks
# ONE at a time to watch from the list, independent of its own
# broadcast state and of what any other viewer is doing.
NO_LIVE_PLACEHOLDER = "── No live streams ──"

def refresh_live_listbox():
    """Refresh the OTHER LIVE STREAMS dropdown with whoever is
    currently broadcasting, keeping the current selection if that
    stream is still live."""
    with live_streams_lock:
        ids = sorted(live_streams.keys())
    current_selection = live_stream_var.get()

    if ids:
        live_dropdown['values'] = ids
        live_dropdown.configure(state='readonly')
        if current_selection in ids:
            live_stream_var.set(current_selection)
        else:
            live_stream_var.set(ids[0])
    else:
        live_dropdown['values'] = [NO_LIVE_PLACEHOLDER]
        live_stream_var.set(NO_LIVE_PLACEHOLDER)
        live_dropdown.configure(state='disabled')
    update_buttons()

def watch_selected_live():
    global view_mode, watching_stream_id
    stream_id = live_stream_var.get()
    if not stream_id or stream_id == NO_LIVE_PLACEHOLDER:
        log("Select a live stream from the dropdown first.")
        return
    with live_streams_lock:
        if stream_id not in live_streams:
            log("That stream just ended.")
            return
    watching_stream_id = stream_id
    view_mode = "WATCH_LIVE"
    log(f"Watching {stream_id}'s live stream.")
    update_buttons()

def stop_watching_live():
    global view_mode, watching_stream_id
    watching_stream_id = None
    if view_mode == "WATCH_LIVE":
        view_mode = "VOD"
    log("Back to video-on-demand view.")
    update_buttons()

# ════════════════════════════════════════════════════════════
#  TIER 3 — RECONNECTION LOGIC
# ════════════════════════════════════════════════════════════
def attempt_reconnect():
    global client_socket, running, current_state, reconnecting
    reconnecting = True
    root.after(0, lambda: set_status("↻ RECONNECTING...", YELLOW))
    log(f"Server dropped. Retrying every {RECONNECT_DELAY}s...")

    for attempt in range(1, MAX_RECONNECTS + 1):
        time.sleep(RECONNECT_DELAY)
        log(f"Reconnect attempt {attempt}/{MAX_RECONNECTS}...")
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((server_ip, PORT))
            client_socket = sock
            running       = True
            current_state = "CONNECTED"
            root.after(0, lambda: set_status("✓ RECONNECTED", ACCENT))
            root.after(0, lambda: log("✓ Reconnected!"))
            root.after(0, update_buttons)
            threading.Thread(target=receive_loop, daemon=True).start()
            reconnecting = False
            return
        except Exception:
            log(f"Attempt {attempt} failed.")

    reconnecting = False
    root.after(0, lambda: set_status("✗ DISCONNECTED", RED))
    root.after(0, lambda: log("Could not reconnect. Click CONNECT to try manually."))
    root.after(0, lambda: connect_btn.configure(state='normal', bg=ACCENT2))
    root.after(0, update_buttons)

# ════════════════════════════════════════════════════════════
#  MAIN SOCKET RECEIVER THREAD (VOD frames + live frames + text)
# ════════════════════════════════════════════════════════════
def handle_text_message(text):
    global current_state, view_mode, watching_stream_id, own_stream_id

    if text.startswith("VIDEOLIST:"):
        raw = text.replace("VIDEOLIST:", "").strip()
        videos = [v for v in raw.split(",") if v]
        populate_video_dropdown(videos)
        return

    if text.startswith("LIVE_STARTED:"):
        stream_id = text.split("LIVE_STARTED:", 1)[1].strip()
        if stream_id == own_stream_id:
            # This is our own start-of-broadcast echo — ignore.
            return
        with live_streams_lock:
            is_new = stream_id not in live_streams
            live_streams[stream_id] = live_streams.get(stream_id)
        if is_new:
            log(f"🔴 {stream_id} just went live.")
        root.after(0, refresh_live_listbox)
        return

    if text.startswith("LIVE_ENDED"):
        parts = text.split(":", 1)
        stream_id = parts[1].strip() if len(parts) > 1 else None
        if stream_id:
            with live_streams_lock:
                live_streams.pop(stream_id, None)
            if watching_stream_id == stream_id:
                watching_stream_id = None
                if view_mode == "WATCH_LIVE":
                    view_mode = "VOD"
                log(f"{stream_id}'s live stream ended — back to video-on-demand.")
        root.after(0, refresh_live_listbox)
        root.after(0, update_buttons)
        return

    # Otherwise treat as an RTSP-style response
    first_line = text.splitlines()[0] if text else ""
    log(f"Server: {first_line}")

    if "Live: GRANTED" in text:
        for line in text.splitlines():
            if line.startswith("StreamID:"):
                own_stream_id = line.split("StreamID:", 1)[1].strip()
        threading.Thread(target=begin_own_live, daemon=True).start()
    elif "Live: DENIED" in text:
        log("Live request denied.")
        root.after(0, update_buttons)
    elif "State: PLAYING" in text:
        set_status("STREAMING", ACCENT)
    elif "State: PAUSED" in text:
        set_status("PAUSED", YELLOW)
    elif "State: STOPPED" in text:
        set_status("STOPPED", RED)

def receive_loop():
    global vod_frame_buffer, running, current_state
    receiver = FrameReceiver()

    while running:
        try:
            packet = client_socket.recv(65536)
            if not packet:
                raise ConnectionError("Server closed connection.")
            receiver.feed(packet)

            for msg_type, payload in receiver.pop_messages():
                if msg_type == TYPE_TEXT:
                    text = payload.decode(errors="ignore")
                    root.after(0, lambda t=text: handle_text_message(t))

                elif msg_type == TYPE_VOD:
                    np_array = np.frombuffer(payload, dtype=np.uint8)
                    frame = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
                    if frame is not None:
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        with buffer_lock:
                            vod_frame_buffer = frame_rgb

                elif msg_type == TYPE_LIVE:
                    try:
                        stream_id, jpeg_bytes = unpack_live_payload(payload)
                    except Exception:
                        continue
                    if stream_id == own_stream_id:
                        continue  # our own relayed frames come back to us too; ignore
                    np_array = np.frombuffer(jpeg_bytes, dtype=np.uint8)
                    frame = cv2.imdecode(np_array, cv2.IMREAD_COLOR)
                    if frame is not None:
                        frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                        with live_streams_lock:
                            live_streams[stream_id] = frame_rgb

        except Exception as e:
            if running:
                log(f"Connection lost: {e}")
                running       = False
                current_state = "IDLE"
                root.after(0, update_buttons)
                if not reconnecting:
                    threading.Thread(target=attempt_reconnect, daemon=True).start()
            break

# ════════════════════════════════════════════════════════════
#  DISPLAY LOOP
# ════════════════════════════════════════════════════════════
def draw_placeholder(msg=""):
    video_canvas.delete("all")
    w = video_canvas.winfo_width()  or 640
    h = video_canvas.winfo_height() or 380
    video_canvas.create_rectangle(0, 0, w, h, fill="#0a0f14")
    for x in range(0, w, 80):
        video_canvas.create_line(x, 0, x, h, fill="#1a2030", width=1)
    for y in range(0, h, 60):
        video_canvas.create_line(0, y, w, y, fill="#1a2030", width=1)
    video_canvas.create_text(w//2, h//2 - 30, text="◈",
                              fill=ACCENT2, font=("Courier New", 40))
    video_canvas.create_text(w//2, h//2 + 20, text=msg,
                              fill=MUTED, font=("Courier New", 11))

def draw_frame(frame, badge=None, badge_color=YELLOW):
    w = video_canvas.winfo_width()  or 640
    h = video_canvas.winfo_height() or 380
    img   = Image.fromarray(frame).resize((w, h), Image.LANCZOS)
    photo = ImageTk.PhotoImage(img)
    video_canvas.photo = photo
    video_canvas.delete("all")
    video_canvas.create_image(0, 0, anchor='nw', image=photo)
    if badge:
        bx1, by1 = w//2 - 140, h//2 - 40
        bx2, by2 = w//2 + 140, h//2 + 40
        video_canvas.create_rectangle(bx1, by1, bx2, by2,
                                       fill="#000000", stipple="gray50")
        video_canvas.create_text(w//2, h//2, text=badge,
                                  fill=badge_color, font=("Courier New", 20, "bold"))

def update_display():
    if view_mode == "OWN_LIVE":
        with buffer_lock:
            frame = local_preview_buf
        if frame is not None:
            try:
                draw_frame(frame, badge="🔴 LIVE (you)", badge_color=RED)
            except Exception as e:
                draw_placeholder(f"Error: {e}")
        else:
            draw_placeholder("Starting camera...")
    elif view_mode == "WATCH_LIVE":
        with live_streams_lock:
            frame = live_streams.get(watching_stream_id)
        if frame is not None:
            try:
                draw_frame(frame)
            except Exception as e:
                draw_placeholder(f"Error: {e}")
        else:
            draw_placeholder(f"Waiting for {watching_stream_id or 'live stream'}...")
    else:
        with buffer_lock:
            frame = vod_frame_buffer

        if current_state == "PLAY" and frame is not None:
            try:
                draw_frame(frame)
            except Exception as e:
                draw_placeholder(f"Error: {e}")
        elif current_state == "PAUSE" and frame is not None:
            try:
                draw_frame(frame, badge="⏸  PAUSED", badge_color=YELLOW)
            except Exception:
                pass
        elif current_state == "CONNECTED":
            draw_placeholder("Connected!   Select a video and click  REQUEST")
        elif current_state == "REQUESTED":
            draw_placeholder("Video selected!   Press  ▶ PLAY  to start")
        elif current_state == "STOP":
            draw_placeholder("Stream stopped.   Press ▶ PLAY to resume, or pick another video")
        else:
            draw_placeholder("Enter server IP and click CONNECT")

    root.after(30, update_display)

# ════════════════════════════════════════════════════════════
#  TIER 3 — FULLSCREEN
# ════════════════════════════════════════════════════════════
def toggle_fullscreen():
    global is_fullscreen
    is_fullscreen = not is_fullscreen
    root.attributes('-fullscreen', is_fullscreen)
    fs_btn.configure(
        text="✕ EXIT FULLSCREEN" if is_fullscreen else "⛶ FULLSCREEN")

root.bind('<Escape>', lambda e: (
    root.attributes('-fullscreen', False),
    globals().update(is_fullscreen=False),
    fs_btn.configure(text="⛶ FULLSCREEN")
))

# ════════════════════════════════════════════════════════════
#  CONNECTION + CONTROL ACTIONS
# ════════════════════════════════════════════════════════════
def connect_to_server():
    global client_socket, running, current_state, server_ip
    ip = ip_var.get().strip()
    if not ip:
        log("Please enter the server IP address.")
        return
    server_ip = ip
    try:
        client_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_socket.connect((ip, PORT))
        running       = True
        current_state = "CONNECTED"
        log(f"Connected to {ip}:{PORT}")
        set_status("CONNECTED", ACCENT2)
        connect_btn.configure(state='disabled', bg=MUTED)
        update_buttons()
        threading.Thread(target=receive_loop, daemon=True).start()
    except Exception as e:
        log(f"Connection failed: {e}")
        set_status("FAILED", RED)

def do_play():
    cut_to_vod()
    send_command("PLAY")
    set_status("STREAMING", ACCENT)

def do_pause():
    send_command("PAUSE")
    set_status("PAUSED", YELLOW)

def do_stop():
    global current_state
    send_command("STOP")
    current_state = "STOP"
    set_status("STOPPED", RED)
    now_playing_var.set("Stopped (still connected)")
    update_buttons()

# ════════════════════════════════════════════════════════════
#  GUI LAYOUT
# ════════════════════════════════════════════════════════════

# ── Title ─────────────────────────────────────────────────────
title_frame = tk.Frame(root, bg=BG)
title_frame.pack(fill='x', padx=20, pady=(16, 0))
tk.Label(title_frame, text="◈ RTSP", font=("Courier New", 22, "bold"),
         bg=BG, fg=BLUE).pack(side='left')
tk.Label(title_frame, text=" VIDEO CLIENT", font=("Courier New", 22, "bold"),
         bg=BG, fg=WHITE).pack(side='left')
tk.Frame(root, bg=BORDER, height=1).pack(fill='x', padx=20, pady=10)

# ── Main area ─────────────────────────────────────────────────
main = tk.Frame(root, bg=BG)
main.pack(fill='both', expand=True, padx=20)
left  = tk.Frame(main, bg=BG)
left.pack(side='left', fill='both', expand=True)
right = tk.Frame(main, bg=BG, width=190)
right.pack(side='right', fill='y', padx=(12, 0))
right.pack_propagate(False)

# ── Video Canvas ──────────────────────────────────────────────
canvas_frame = styled_frame(left)
canvas_frame.pack(fill='both', expand=True)
video_canvas = tk.Canvas(canvas_frame, width=640, height=360,
                          bg="#0a0f14", highlightthickness=0)
video_canvas.pack(fill='both', expand=True, padx=2, pady=2)
draw_placeholder("Enter server IP and click CONNECT")

# ── Video Selection Row ───────────────────────────────────────
sel_frame = styled_frame(left)
sel_frame.pack(fill='x', pady=6)
tk.Label(sel_frame, text="  SELECT VIDEO", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(anchor='w', padx=12, pady=(10, 4))
sel_row = tk.Frame(sel_frame, bg=PANEL)
sel_row.pack(fill='x', padx=12, pady=(0, 10))

video_var = tk.StringVar(value="── Select a video ──")
video_dropdown = ttk.Combobox(sel_row, textvariable=video_var,
    font=FONT_LABEL, state='readonly', width=32)
video_dropdown['values'] = ["── Connect to server first ──"]

style = ttk.Style()
style.theme_use('clam')
style.configure("TCombobox",
    fieldbackground="#0d1117", background=PANEL,
    foreground=WHITE, selectbackground=ACCENT2,
    bordercolor=BORDER, arrowcolor=ACCENT)
video_dropdown.pack(side='left', padx=(0, 10), ipady=4)

request_btn = tk.Button(sel_row, text="REQUEST VIDEO", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=14, pady=4, state='disabled', command=request_video)
request_btn.pack(side='left')

now_playing_var = tk.StringVar(value="Nothing playing")
tk.Label(sel_row, textvariable=now_playing_var, font=FONT_BOLD,
         bg=PANEL, fg=YELLOW).pack(side='right', padx=(0, 4))

nav_row = tk.Frame(sel_frame, bg=PANEL)
nav_row.pack(fill='x', padx=12, pady=(0, 10))
prev_btn = tk.Button(nav_row, text="◀ PREVIOUS", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=14, pady=4, state='disabled', command=do_previous)
prev_btn.pack(side='left', padx=(0, 6))
next_btn = tk.Button(nav_row, text="NEXT ▶", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=14, pady=4, state='disabled', command=do_next)
next_btn.pack(side='left')
tk.Label(nav_row, text="Jumps to and immediately plays the next/previous video",
    font=("Courier New", 8), bg=PANEL, fg=MUTED).pack(side='left', padx=(10, 0))

# ── Connection Row ────────────────────────────────────────────
conn_frame = styled_frame(left)
conn_frame.pack(fill='x', pady=6)
conn_row = tk.Frame(conn_frame, bg=PANEL)
conn_row.pack(fill='x', padx=12, pady=10)
tk.Label(conn_row, text="SERVER IP:", font=FONT_BOLD,
         bg=PANEL, fg=MUTED).pack(side='left', padx=(0, 8))
ip_var = tk.StringVar(value="127.0.0.1")
tk.Entry(conn_row, textvariable=ip_var, font=FONT_LABEL,
         bg="#0d1117", fg=WHITE, insertbackground=WHITE,
         relief='flat', width=16,
         highlightbackground=BORDER, highlightthickness=1
         ).pack(side='left', padx=(0, 10), ipady=4)
connect_btn = tk.Button(conn_row, text="CONNECT", font=FONT_BOLD,
    bg=ACCENT2, fg=WHITE, relief='flat', cursor='hand2',
    padx=14, pady=4, command=connect_to_server)
connect_btn.pack(side='left')
f2 = tk.Frame(conn_row, bg=PANEL)
f2.pack(side='right', padx=(0, 4))
tk.Label(f2, text="STATUS", font=FONT_LABEL, bg=PANEL, fg=MUTED).pack(anchor='e')
status_var = tk.StringVar(value="IDLE")
status_label = tk.Label(f2, textvariable=status_var, font=FONT_STATUS,
                         bg=PANEL, fg=MUTED)
status_label.pack(anchor='e')

# ── Playback Controls ─────────────────────────────────────────
ctrl_frame = styled_frame(left)
ctrl_frame.pack(fill='x', pady=(0, 8))
ctrl_row = tk.Frame(ctrl_frame, bg=PANEL)
ctrl_row.pack(pady=12, padx=12)

play_btn = tk.Button(ctrl_row, text="▶  PLAY", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=16, pady=10, state='disabled', command=do_play)
play_btn.pack(side='left', padx=(0, 6))

pause_btn = tk.Button(ctrl_row, text="⏸  PAUSE", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=16, pady=10, state='disabled', command=do_pause)
pause_btn.pack(side='left', padx=(0, 6))

stop_btn = tk.Button(ctrl_row, text="■  STOP", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=16, pady=10, state='disabled', command=do_stop)
stop_btn.pack(side='left', padx=(0, 6))

fs_btn = tk.Button(ctrl_row, text="⛶ FULLSCREEN", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=16, pady=10, state='disabled', command=toggle_fullscreen)
fs_btn.pack(side='left', padx=(0, 6))

tk.Button(ctrl_row, text="QUIT", font=FONT_BOLD,
    bg=PANEL, fg=MUTED, relief='flat', cursor='hand2',
    padx=14, pady=10, command=root.quit).pack(side='right')

# ── Live Controls Row ─────────────────────────────────────────
# Any number of clients can be live at once: this client's own
# GO LIVE / END LIVE toggle is independent of who else is broadcasting,
# and the list below shows every OTHER client currently live so this
# client can pick (at most) one of them to watch.
live_frame = styled_frame(left)
live_frame.pack(fill='x', pady=(0, 8))
live_row = tk.Frame(live_frame, bg=PANEL)
live_row.pack(pady=(10, 4), padx=12, fill='x')

live_btn = tk.Button(live_row, text="🔴  GO LIVE", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=16, pady=8, state='disabled', command=toggle_own_live)
live_btn.pack(side='left', padx=(0, 6))

tk.Label(live_row, text="OTHER LIVE STREAMS:", font=FONT_LABEL,
          bg=PANEL, fg=MUTED).pack(side='left', padx=(14, 6))

live_stream_var = tk.StringVar(value="── No live streams ──")
live_dropdown = ttk.Combobox(live_row, textvariable=live_stream_var,
    font=FONT_LABEL, state='disabled', width=18)
live_dropdown['values'] = ["── No live streams ──"]
live_dropdown.pack(side='left', padx=(0, 6), ipady=2)

watch_btn = tk.Button(live_row, text="▶ WATCH", font=FONT_BOLD,
    bg=MUTED, fg="#0d1117", relief='flat', cursor='hand2',
    padx=12, pady=8, state='disabled', command=watch_selected_live)
watch_btn.pack(side='left', padx=(0, 6))

stop_watch_btn = tk.Button(live_row, text="◀ BACK TO VOD", font=FONT_BOLD,
    bg=MUTED, fg=WHITE, relief='flat', cursor='hand2',
    padx=12, pady=8, state='disabled', command=stop_watching_live)
stop_watch_btn.pack(side='left')

# ── Log Panel ─────────────────────────────────────────────────
tk.Label(right, text="CLIENT LOG", font=FONT_BOLD,
         bg=BG, fg=MUTED).pack(anchor='w', pady=(0, 6))
log_frame = styled_frame(right)
log_frame.pack(fill='both', expand=True)
log_box = scrolledtext.ScrolledText(log_frame, font=FONT_LOG,
    bg="#0d1117", fg=BLUE, insertbackground=BLUE,
    relief='flat', wrap='word', state='disabled',
    selectbackground=ACCENT2, width=22)
log_box.pack(fill='both', expand=True, padx=6, pady=6)

# ── Guide ─────────────────────────────────────────────────────
guide = tk.Frame(root, bg=BG)
guide.pack(fill='x', padx=20, pady=(0, 12))
tk.Label(guide,
    text="  P → PLAY   |   S → PAUSE   |   Q → STOP   |   N → NEXT   |   B → PREVIOUS   |   F → FULLSCREEN   |   ESC → EXIT",
    font=("Courier New", 9), bg=BG, fg=MUTED).pack(anchor='w')

# ── Keyboard shortcuts ────────────────────────────────────────
root.bind('<p>', lambda e: do_play()           if play_btn['state']    == 'normal' else None)
root.bind('<s>', lambda e: do_pause()          if pause_btn['state']   == 'normal' else None)
root.bind('<q>', lambda e: do_stop()           if stop_btn['state']    == 'normal' else None)
root.bind('<f>', lambda e: toggle_fullscreen() if fs_btn['state']      == 'normal' else None)
root.bind('<r>', lambda e: request_video()     if request_btn['state'] == 'normal' else None)
root.bind('<n>', lambda e: do_next()           if next_btn['state']    == 'normal' else None)
root.bind('<b>', lambda e: do_previous()       if prev_btn['state']    == 'normal' else None)

log("Client GUI ready. Enter server IP and click CONNECT.")
update_buttons()
update_display()
root.mainloop()
