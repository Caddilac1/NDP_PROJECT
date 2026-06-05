import socket
import threading
import urllib.parse
import tkinter as tk
from tkinter import font as tkfont
from PIL import Image, ImageTk
import numpy as np
import subprocess
import os
import tempfile
import queue

try:
    import signal as _signal
except ImportError:
    _signal = None

# ── Constants ─────────────────────────────────────────────────────────────────
RTSP_BUFFER_SIZE  = 4096
RTSP_PORT_DEFAULT = 8554
RTP_PORT          = 5004
AUDIO_RTP_PORT    = 5020   # 5004 + 16, well clear of all video RTCP ports
CRLF              = "\r\n"

STATE_DISCONNECTED = "DISCONNECTED"
STATE_CONNECTED    = "CONNECTED"
STATE_READY        = "READY"
STATE_PLAYING      = "PLAYING"

# ── Global state ──────────────────────────────────────────────────────────────
rtsp_socket   = None
session_id    = None
rtsp_host     = "127.0.0.1"
rtsp_port     = RTSP_PORT_DEFAULT
rtsp_path     = "Download.mp4"
rtsp_state    = STATE_DISCONNECTED

video_width   = 0
video_height  = 0
has_audio_sdp = False
audio_port    = AUDIO_RTP_PORT

ffmpeg_video_proc = None
ffplay_proc       = None
video_thread      = None
video_stop_event  = threading.Event()
_audio_sdp_file   = None

# Size-1 queue: reader always replaces stale frame so display never lags
_frame_queue = queue.Queue(maxsize=1)

_cseq = 1


# ═══════════════════════════════════════════════════════════════════════════════
#  Status helpers
# ═══════════════════════════════════════════════════════════════════════════════
def update_status(text: str, color: str = "#e0e0e0"):
    connection_status.config(text=text, fg=color)

def schedule_status(text: str, color: str = "#e0e0e0"):
    root.after(0, update_status, text, color)


# ═══════════════════════════════════════════════════════════════════════════════
#  URL parsing
# ═══════════════════════════════════════════════════════════════════════════════
def parse_rtsp_url(url: str):
    parsed = urllib.parse.urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port    or RTSP_PORT_DEFAULT
    path = parsed.path    or "/Download.mp4"
    if path.startswith("/"):
        path = path[1:]
    if not path:
        path = "Download.mp4"
    return host, port, path


# ═══════════════════════════════════════════════════════════════════════════════
#  RTSP helpers
# ═══════════════════════════════════════════════════════════════════════════════
def receive_rtsp_response(sock: socket.socket):
    data = b""
    while (CRLF + CRLF).encode() not in data:
        try:
            chunk = sock.recv(RTSP_BUFFER_SIZE)
        except socket.timeout:
            return None
        if not chunk:
            break
        data += chunk
    return data.decode("utf-8", errors="ignore")


def _parse_sdp_dimensions(sdp: str):
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("a=x-dimensions:"):
            try:
                w, h = line.split(":")[1].split(",")
                return int(w), int(h)
            except Exception:
                pass
    return None, None


def _sdp_has_audio(sdp: str) -> bool:
    return "m=audio" in sdp


def _parse_audio_port_from_sdp(sdp: str) -> int:
    for line in sdp.splitlines():
        line = line.strip()
        if line.startswith("m=audio"):
            try:
                return int(line.split()[1])
            except Exception:
                pass
    return AUDIO_RTP_PORT


# ═══════════════════════════════════════════════════════════════════════════════
#  Connect
# ═══════════════════════════════════════════════════════════════════════════════
def connect():
    global rtsp_socket, rtsp_host, rtsp_port, rtsp_path, rtsp_state
    global video_width, video_height, has_audio_sdp, audio_port
    video_width   = 0
    video_height  = 0
    has_audio_sdp = False
    audio_port    = AUDIO_RTP_PORT

    url = entry.get().strip()
    if not url:
        update_status("Enter a valid RTSP URL", "#ff6b6b")
        return
    try:
        rtsp_host, rtsp_port, rtsp_path = parse_rtsp_url(url)
        rtsp_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        rtsp_socket.settimeout(5.0)
        rtsp_socket.connect((rtsp_host, rtsp_port))
        rtsp_state = STATE_CONNECTED
        update_status(f"Connected  {rtsp_host}:{rtsp_port}", "#69db7c")
        video_label.config(text="Connected — click Describe then Setup then Play", image="")
    except Exception as exc:
        update_status(f"Connection failed: {exc}", "#ff6b6b")
        rtsp_socket = None
        rtsp_state  = STATE_DISCONNECTED


# ═══════════════════════════════════════════════════════════════════════════════
#  RTSP request sender
# ═══════════════════════════════════════════════════════════════════════════════
def send_rtsp_request(method: str, extra_headers: dict = None):
    global session_id, _cseq

    if rtsp_socket is None:
        schedule_status("Not connected", "#ff6b6b")
        return None

    lines = [
        f"{method} rtsp://{rtsp_host}/{rtsp_path} RTSP/1.0",
        f"CSeq: {_cseq}",
    ]

    if method == "SETUP":
        lines.append(f"Transport: RTP/UDP;unicast;client_port={RTP_PORT}-{RTP_PORT+1}")
    elif session_id:
        lines.append(f"Session: {session_id}")

    if extra_headers:
        for k, v in extra_headers.items():
            lines.append(f"{k}: {v}")

    request = CRLF.join(lines) + CRLF + CRLF
    try:
        rtsp_socket.sendall(request.encode("utf-8"))
        response = receive_rtsp_response(rtsp_socket)
        if response is None:
            schedule_status(f"RTSP {method} timed out", "#ff6b6b")
            return None
        print(f"[RTSP] Response for {method}:\n{response}")
        if "Session:" in response:
            for line in response.splitlines():
                if line.startswith("Session:"):
                    session_id = line.split(":", 1)[1].strip().split(";")[0]
                    break
        _cseq += 1
        return response
    except socket.timeout:
        schedule_status(f"RTSP {method} timed out", "#ff6b6b")
        return None
    except Exception as exc:
        schedule_status(f"RTSP {method} failed: {exc}", "#ff6b6b")
        return None


# ═══════════════════════════════════════════════════════════════════════════════
#  Video — ffmpeg pipe decoder
# ═══════════════════════════════════════════════════════════════════════════════
def _probe_video_dimensions_from_rtp(sdp_path: str):
    try:
        r = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-protocol_whitelist", "file,udp,rtp",
                "-i", sdp_path,
                "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "csv=p=0",
            ],
            capture_output=True, text=True, timeout=8
        )
        parts = r.stdout.strip().split(",")
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            return int(parts[0]), int(parts[1])
    except Exception as e:
        print(f"[Video] ffprobe dims failed: {e}")
    return 0, 0


def _ffmpeg_video_reader():
    global ffmpeg_video_proc, video_width, video_height

    root.after(0, update_frame, None, "Waiting for video stream…")

    sdp_content = (
        "v=0\r\n"
        f"o=- 0 0 IN IP4 {rtsp_host}\r\n"
        "s=Video\r\n"
        f"c=IN IP4 {rtsp_host}\r\n"
        "t=0 0\r\n"
        f"m=video {RTP_PORT} RTP/AVP 96\r\n"
        "a=rtpmap:96 H264/90000\r\n"
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sdp", delete=False, prefix="rtsp_video_"
    )
    tmp.write(sdp_content)
    tmp.close()
    sdp_path = tmp.name

    w, h = video_width, video_height
    if w == 0 or h == 0:
        print("[Video] Dimensions unknown — probing from RTP stream …")
        w, h = _probe_video_dimensions_from_rtp(sdp_path)
        if w == 0 or h == 0:
            print("[Video] Probe failed, falling back to 1280x720")
            w, h = 1280, 720
        else:
            print(f"[Video] Probed from stream: {w}x{h}")
        video_width, video_height = w, h

    frame_size = w * h * 3  # BGR24

    cmd = [
        "ffmpeg",
        "-loglevel", "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-buffer_size", "2097152",
        "-reorder_queue_size", "0",
        "-fflags", "nobuffer",
        "-flags", "low_delay",
        "-probesize", "32",
        "-analyzeduration", "0",
        "-i", sdp_path,
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-vf", f"scale={w}:{h}",
        "pipe:1",
    ]
    print(f"[Video] ffmpeg cmd: {' '.join(cmd)}")

    kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000
    elif _signal:
        kwargs["preexec_fn"] = os.setsid

    ffmpeg_video_proc = subprocess.Popen(cmd, **kwargs)
    threading.Thread(
        target=_log_stderr, args=(ffmpeg_video_proc, "Video"), daemon=True
    ).start()

    while not video_stop_event.is_set():
        raw = ffmpeg_video_proc.stdout.read(frame_size)
        if len(raw) < frame_size:
            print(f"[Video] Short read ({len(raw)} bytes) — stream ended.")
            break
        frame = np.frombuffer(raw, dtype=np.uint8).reshape((h, w, 3))
        # Drop stale frame, keep only the newest
        try:
            _frame_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            _frame_queue.put_nowait(frame)
        except queue.Full:
            pass

    try:
        os.remove(sdp_path)
    except Exception:
        pass

    if not video_stop_event.is_set():
        root.after(0, _on_video_ended)


def _poll_frame():
    """Tkinter polls every 33 ms — always shows the newest frame, drops old ones."""
    if not video_stop_event.is_set():
        try:
            frame = _frame_queue.get_nowait()
            _display_frame(frame)
        except queue.Empty:
            pass
        root.after(33, _poll_frame)


def _display_frame(frame_bgr):
    frame_rgb = frame_bgr[:, :, ::-1]
    pil_img   = Image.fromarray(frame_rgb)

    vw = video_frame.winfo_width()  or 980
    vh = video_frame.winfo_height() or 540
    scale   = min(vw / pil_img.width, vh / pil_img.height, 1.0)
    new_w   = max(1, int(pil_img.width  * scale))
    new_h   = max(1, int(pil_img.height * scale))
    pil_img = pil_img.resize((new_w, new_h), Image.LANCZOS)

    photo = ImageTk.PhotoImage(pil_img)
    video_label.config(image=photo, text="")
    video_label.image = photo


def _on_video_ended():
    video_label.config(text="Stream ended", image="")
    schedule_status("Stream ended", "#e0e0e0")


def start_video_thread():
    global video_thread
    video_stop_event.clear()
    # Drain leftover frames from previous session
    while not _frame_queue.empty():
        try:
            _frame_queue.get_nowait()
        except queue.Empty:
            break
    if video_thread is None or not video_thread.is_alive():
        video_thread = threading.Thread(target=_ffmpeg_video_reader, daemon=True)
        video_thread.start()
    root.after(33, _poll_frame)


def stop_video():
    """Kill the client-side ffmpeg decoder. Call on both PAUSE and TEARDOWN."""
    global ffmpeg_video_proc
    video_stop_event.set()
    if ffmpeg_video_proc and ffmpeg_video_proc.poll() is None:
        try:
            if os.name == "nt":
                ffmpeg_video_proc.terminate()
            elif _signal:
                os.killpg(os.getpgid(ffmpeg_video_proc.pid), _signal.SIGTERM)
            else:
                ffmpeg_video_proc.terminate()
        except Exception as e:
            print(f"[Video] Stop error: {e}")
        try:
            ffmpeg_video_proc.wait(timeout=3)
        except Exception:
            ffmpeg_video_proc.kill()
        ffmpeg_video_proc = None


# ═══════════════════════════════════════════════════════════════════════════════
#  Audio — ffplay reads SDP directly
# ═══════════════════════════════════════════════════════════════════════════════
def _write_audio_sdp(host: str, port: int) -> str:
    sdp = (
        "v=0\r\n"
        f"o=- 0 0 IN IP4 {host}\r\n"
        "s=Audio\r\n"
        f"c=IN IP4 {host}\r\n"
        "t=0 0\r\n"
        f"m=audio {port} RTP/AVP 14\r\n"
        "b=AS:128\r\n"
        "a=rtpmap:14 MPA/90000\r\n"
    )
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".sdp", delete=False, prefix="rtsp_audio_"
    )
    tmp.write(sdp)
    tmp.close()
    print(f"[Audio] SDP written to {tmp.name} (port {port})")
    return tmp.name


def start_audio():
    global ffplay_proc, _audio_sdp_file
    stop_audio()

    if not has_audio_sdp:
        print("[Audio] No audio track — skipping.")
        return

    _audio_sdp_file = _write_audio_sdp(rtsp_host, audio_port)

    cmd = [
        "ffplay",
        "-loglevel", "warning",
        "-protocol_whitelist", "file,udp,rtp",
        "-i", _audio_sdp_file,
        "-nodisp",
        "-vn",
        "-infbuf",
        "-sync", "ext",
        "-af", "aresample=async=1",
    ]

    popen_kw = {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    if os.name == "nt":
        popen_kw["creationflags"] = 0x08000000

    print(f"[Audio] cmd: {' '.join(cmd)}")
    try:
        ffplay_proc = subprocess.Popen(cmd, **popen_kw)
        threading.Thread(
            target=_log_stderr, args=(ffplay_proc, "Audio"), daemon=True
        ).start()
        print(f"[Audio] ffplay PID {ffplay_proc.pid}")
    except FileNotFoundError:
        print("[Audio] ffplay not found — make sure ffmpeg is installed and on PATH")
    except Exception as e:
        print(f"[Audio] Failed: {e}")


def stop_audio():
    global ffplay_proc, _audio_sdp_file
    if ffplay_proc and ffplay_proc.poll() is None:
        try:
            ffplay_proc.terminate()
            ffplay_proc.wait(timeout=2)
        except Exception as e:
            print(f"[Audio] Stop error: {e}")
    ffplay_proc = None
    if _audio_sdp_file and os.path.exists(_audio_sdp_file):
        try:
            os.remove(_audio_sdp_file)
        except Exception:
            pass
        _audio_sdp_file = None
    print("[Audio] Stopped.")


def _log_stderr(proc, label):
    for line in proc.stderr:
        print(f"[{label}] {line.decode(errors='ignore').rstrip()}")
    print(f"[{label}] process ended (code {proc.wait()})")


# ═══════════════════════════════════════════════════════════════════════════════
#  Frame display fallback
# ═══════════════════════════════════════════════════════════════════════════════
def update_frame(frame=None, text=None):
    if frame is not None:
        _display_frame(frame)
    elif text:
        video_label.config(text=text, image="")


# ═══════════════════════════════════════════════════════════════════════════════
#  RTSP command runner
# ═══════════════════════════════════════════════════════════════════════════════
def run_rtsp_command(method: str):
    global rtsp_state, rtsp_socket, session_id
    global video_width, video_height, has_audio_sdp, audio_port

    if method == "DESCRIBE":
        response = send_rtsp_request("DESCRIBE", {"Accept": "application/sdp"})
        if response and "200 OK" in response:
            sdp_body = response.split(CRLF + CRLF, 1)[1] if CRLF + CRLF in response else ""
            w, h = _parse_sdp_dimensions(sdp_body)
            if w and h:
                video_width, video_height = w, h
                print(f"[Client] Dimensions from SDP: {w}x{h}")
            has_audio_sdp = _sdp_has_audio(sdp_body)
            audio_port    = _parse_audio_port_from_sdp(sdp_body)
            print(f"[Client] Audio={has_audio_sdp} port={audio_port}")
            schedule_status("DESCRIBE OK — click Setup", "#69db7c")
        else:
            schedule_status("DESCRIBE failed", "#ff6b6b")
        return

    response = send_rtsp_request(method)
    if response is None:
        return

    if method == "SETUP":
        if "200 OK" in response:
            rtsp_state = STATE_READY
            schedule_status("Setup OK — click Play", "#69db7c")
            root.after(0, update_frame, None, "Ready — click Play")
        else:
            schedule_status("SETUP failed", "#ff6b6b")

    elif method == "PLAY":
        if "200 OK" in response:
            rtsp_state = STATE_PLAYING
            schedule_status("▶  Playing", "#69db7c")
            root.after(0, start_video_thread)
            root.after(2500, start_audio)
        else:
            schedule_status("PLAY failed", "#ff6b6b")

    elif method == "PAUSE":
        if "200 OK" in response:
            rtsp_state = STATE_READY
            # Stop both audio AND video decoder on pause.
            # The server has stopped sending RTP so ffmpeg would time out
            # anyway — stopping it cleanly prevents "stream ended" appearing.
            stop_audio()
            stop_video()
            root.after(0, update_frame, None, "⏸  Paused — click Play to resume")
            schedule_status("⏸  Paused", "#ffd43b")
        else:
            schedule_status("PAUSE failed", "#ff6b6b")

    elif method == "TEARDOWN":
        stop_audio()
        stop_video()
        if "200 OK" in response:
            schedule_status("Teardown complete", "#e0e0e0")
        else:
            schedule_status("TEARDOWN done", "#e0e0e0")
        if rtsp_socket:
            try:
                rtsp_socket.close()
            except Exception:
                pass
        rtsp_socket = None
        session_id  = None
        rtsp_state  = STATE_DISCONNECTED
        root.after(0, update_frame, None, "Stream closed")


# ═══════════════════════════════════════════════════════════════════════════════
#  Button handlers
# ═══════════════════════════════════════════════════════════════════════════════
def do_connect():
    connect()

def do_describe():
    if rtsp_state != STATE_CONNECTED:
        update_status("Connect first", "#ff6b6b")
        return
    threading.Thread(target=run_rtsp_command, args=("DESCRIBE",), daemon=True).start()

def do_setup():
    if rtsp_state not in (STATE_CONNECTED, STATE_READY):
        update_status("Connect first", "#ff6b6b")
        return
    threading.Thread(target=run_rtsp_command, args=("SETUP",), daemon=True).start()

def do_play():
    if rtsp_state != STATE_READY:
        update_status("Run Setup before Play", "#ff6b6b")
        return
    threading.Thread(target=run_rtsp_command, args=("PLAY",), daemon=True).start()

def do_pause():
    if rtsp_state != STATE_PLAYING:
        update_status("Not playing", "#ff6b6b")
        return
    threading.Thread(target=run_rtsp_command, args=("PAUSE",), daemon=True).start()

def do_teardown():
    if rtsp_socket is None and rtsp_state == STATE_DISCONNECTED:
        update_status("Nothing to tear down", "#ff6b6b")
        return
    threading.Thread(target=run_rtsp_command, args=("TEARDOWN",), daemon=True).start()


# ═══════════════════════════════════════════════════════════════════════════════
#  GUI
# ═══════════════════════════════════════════════════════════════════════════════
root = tk.Tk()
root.title("Group 5 — RTSP Player")
root.geometry("1280x780")
root.configure(bg="#0f0f13")
root.resizable(True, True)

try:
    TITLE_FONT  = tkfont.Font(family="Consolas", size=13, weight="bold")
    LABEL_FONT  = tkfont.Font(family="Consolas", size=10)
    BUTTON_FONT = tkfont.Font(family="Consolas", size=10, weight="bold")
    STATUS_FONT = tkfont.Font(family="Consolas", size=10)
except Exception:
    TITLE_FONT  = ("TkFixedFont", 13, "bold")
    LABEL_FONT  = ("TkFixedFont", 10)
    BUTTON_FONT = ("TkFixedFont", 10, "bold")
    STATUS_FONT = ("TkFixedFont", 10)

BG        = "#0f0f13"
PANEL_BG  = "#1a1a22"
BORDER    = "#2e2e3e"
ACCENT    = "#00d4ff"
BTN_BG    = "#1f1f2e"
BTN_FG    = "#c8c8d4"
BTN_HOVER = "#2a2a3e"
FG        = "#e0e0e0"

top_frame = tk.Frame(root, bg=PANEL_BG, pady=10)
top_frame.pack(fill="x")

inner_top = tk.Frame(top_frame, bg=PANEL_BG)
inner_top.pack(padx=16)

tk.Label(inner_top, text="RTSP", bg=PANEL_BG, fg=ACCENT, font=TITLE_FONT
         ).pack(side="left", padx=(0, 8))
tk.Label(inner_top, text="Stream URL:", bg=PANEL_BG, fg=FG, font=LABEL_FONT
         ).pack(side="left", padx=(0, 5))

entry = tk.Entry(
    inner_top, width=52,
    bg="#12121a", fg=ACCENT, insertbackground=ACCENT,
    relief="flat", bd=0, highlightthickness=1,
    highlightbackground=BORDER, highlightcolor=ACCENT,
    font=LABEL_FONT
)
entry.pack(side="left", padx=5, ipady=4)
entry.insert(0, "rtsp://127.0.0.1:8554/Download.mp4")

tk.Button(
    inner_top, text="Connect", command=do_connect,
    bg=ACCENT, fg="#0f0f13",
    activebackground="#00afd4", activeforeground="#0f0f13",
    relief="flat", bd=0, cursor="hand2",
    font=BUTTON_FONT, padx=12, pady=4
).pack(side="left", padx=(10, 4))

connection_status = tk.Label(
    inner_top, text="●  Disconnected",
    bg=PANEL_BG, fg="#ff6b6b", font=STATUS_FONT
)
connection_status.pack(side="left", padx=12)

ctrl_frame = tk.Frame(root, bg=PANEL_BG, pady=10)
ctrl_frame.pack(fill="x", side="bottom")

footer = tk.Frame(root, bg=BG)
footer.pack(fill="x", pady=(0, 4), side="bottom")
tk.Label(
    footer,
    text="Connect → Describe → Setup → Play  |  H.264+Audio / RTP/UDP",
    bg=BG, fg="#333344", font=("Consolas", 9)
).pack()

ctrl_inner = tk.Frame(ctrl_frame, bg=PANEL_BG)
ctrl_inner.pack()

for label, cmd in [
    ("Describe",    do_describe),
    ("Setup",       do_setup),
    ("▶  Play",     do_play),
    ("⏸  Pause",    do_pause),
    ("■  Teardown", do_teardown),
]:
    tk.Button(
        ctrl_inner, text=label, command=cmd,
        bg=BTN_BG, fg=BTN_FG,
        activebackground=BTN_HOVER, activeforeground=ACCENT,
        relief="flat", bd=0, cursor="hand2",
        font=BUTTON_FONT, padx=18, pady=6,
        highlightthickness=1, highlightbackground=BORDER
    ).pack(side="left", padx=5)

video_outer = tk.Frame(root, bg=BORDER, padx=2, pady=2)
video_outer.pack(fill="both", expand=True, padx=16, pady=(8, 0))

video_frame = tk.Frame(video_outer, bg="#000000")
video_frame.pack(fill="both", expand=True)

video_label = tk.Label(
    video_frame,
    text="No stream — connect and press Play",
    bg="#000000", fg="#444455",
    font=("Consolas", 14),
    wraplength=960
)
video_label.pack(expand=True, fill="both")

root.mainloop()