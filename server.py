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
