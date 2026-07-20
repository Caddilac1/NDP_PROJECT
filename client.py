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
