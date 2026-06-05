import socket
import threading
import random
import subprocess
import os
import signal

RTSP_PORT = 8554
CRLF = "\r\n"

STATE_INIT = 0
STATE_READY = 1
STATE_PLAYING = 2

# Audio port offset — must match AUDIO_RTP_PORT - RTP_PORT in client.py
# Client video=5004, audio=5020, so offset=16
AUDIO_PORT_OFFSET = 16


class RTSPServerWorker(threading.Thread):
    def __init__(self, client_socket: socket.socket, client_address):
        super().__init__(daemon=True)
        self.client_socket = client_socket
        self.client_address = client_address
        self.state = STATE_INIT
        self.session_id = str(random.randint(100000, 999999))
        self.video_file = None
        self.client_rtp_port = None
        self.ffmpeg_proc = None
        self.cseq = 1

    def run(self):
        print(f"[RTSP] Client connected: {self.client_address}")
        self.client_socket.settimeout(30.0)
        try:
            buf = ""
            while True:
                try:
                    chunk = self.client_socket.recv(4096).decode("utf-8", errors="ignore")
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while CRLF + CRLF in buf:
                    msg, buf = buf.split(CRLF + CRLF, 1)
                    print(f"\n[RTSP] >> Request:\n{msg}\n")
                    self.handle_rtsp_request(msg)
        except (ConnectionResetError, OSError):
            pass
        finally:
            self._stop_ffmpeg()
            self.client_socket.close()
            print(f"[RTSP] Client {self.client_address} disconnected.")

    def handle_rtsp_request(self, message: str):
        lines = message.split(CRLF)
        if not lines:
            return
        parts = lines[0].split()
        if len(parts) < 2:
            return
        method = parts[0]

        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        cseq = headers.get("cseq", str(self.cseq))

        dispatch = {
            "OPTIONS":  self.handle_options,
            "DESCRIBE": self.handle_describe,
            "SETUP":    self.handle_setup,
            "PLAY":     self.handle_play,
            "PAUSE":    self.handle_pause,
            "TEARDOWN": self.handle_teardown,
        }
        handler = dispatch.get(method)
        if handler:
            handler(parts, headers, cseq)
        else:
            self.send_response(501, cseq)

    def handle_options(self, parts, headers, cseq):
        self.send_response(200, cseq, {
            "Public": "OPTIONS, DESCRIBE, SETUP, PLAY, PAUSE, TEARDOWN"
        })

    def handle_describe(self, parts, headers, cseq):
        url = parts[1] if len(parts) > 1 else ""
        self.video_file = self._extract_path(url)

        if not os.path.isfile(self.video_file):
            print(f"[RTSP] File not found: {self.video_file}")
            self.send_response(404, cseq)
            return

        has_audio = self._probe_has_audio(self.video_file)
        w, h = self._probe_dimensions(self.video_file)
        print(f"[RTSP] Video: {self.video_file}  {w}x{h}  audio={has_audio}")

        sdp = self._build_sdp(has_audio, w, h)
        self.send_response(200, cseq, {
            "Content-Type":   "application/sdp",
            "Content-Length": str(len(sdp)),
        }, body=sdp)

    def handle_setup(self, parts, headers, cseq):
        if self.state not in (STATE_INIT, STATE_READY):
            self.send_response(455, cseq, {"Session": self.session_id})
            return

        url = parts[1] if len(parts) > 1 else ""
        transport = headers.get("transport", "")
        client_port = self._parse_client_port(transport)
        self.client_rtp_port = client_port

        if self.video_file is None:
            self.video_file = self._extract_path(url)

        self.state = STATE_READY
        print(f"[RTSP] SETUP: client_rtp_port={client_port}")

        self.send_response(200, cseq, {
            "Session":   self.session_id,
            "Transport": (
                f"RTP/UDP;unicast;"
                f"client_port={client_port}-{client_port+1};"
                f"server_port={client_port+1}-{client_port+2}"
            ),
        })

    def handle_play(self, parts, headers, cseq):
        if self.state != STATE_READY:
            self.send_response(455, cseq, {"Session": self.session_id})
            return

        self.state = STATE_PLAYING
        self.send_response(200, cseq, {
            "Session":  self.session_id,
            "RTP-Info": f"url=rtsp://{self.client_address[0]}/{self.video_file};seq=1;rtptime=0",
        })
        self._start_ffmpeg()

    def handle_pause(self, parts, headers, cseq):
        if self.state != STATE_PLAYING:
            self.send_response(455, cseq, {"Session": self.session_id})
            return
        self._stop_ffmpeg()
        self.state = STATE_READY
        self.send_response(200, cseq, {"Session": self.session_id})

    def handle_teardown(self, parts, headers, cseq):
        self._stop_ffmpeg()
        self.state = STATE_INIT
        self.send_response(200, cseq, {"Session": self.session_id})

    def _audio_port(self):
        return (self.client_rtp_port or 5004) + AUDIO_PORT_OFFSET

    def _start_ffmpeg(self):
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            return

        client_ip  = self.client_address[0]
        video_port = self.client_rtp_port or 5004
        audio_port = self._audio_port()
        has_audio  = self._probe_has_audio(self.video_file)
        w, h       = self._probe_dimensions(self.video_file)

        print(f"[DEBUG] video_port={video_port}, audio_port={audio_port}, has_audio={has_audio}")

        cmd = [
            "ffmpeg",
            "-re",
            "-i", self.video_file,
            "-map", "0:v:0",
            "-vcodec", "libx264",
            "-preset", "ultrafast",
            "-tune", "zerolatency",
            "-s", f"{w}x{h}",
            "-b:v", "800k",
            "-maxrate", "900k",
            "-bufsize", "1600k",
            "-g", "60",
            "-f", "rtp",
            f"rtp://{client_ip}:{video_port}",
        ]

        if has_audio:
            cmd += [
                "-map", "0:a:0",
                "-acodec", "libmp3lame",
                "-b:a", "128k",
                "-ar", "44100",
                "-ac", "2",
                "-f", "rtp",
                f"rtp://{client_ip}:{audio_port}",
            ]

        print(f"[FFmpeg] Starting: {' '.join(cmd)}")

        kwargs = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.PIPE,
        }
        if os.name != "nt":
            kwargs["preexec_fn"] = os.setsid
        else:
            kwargs["creationflags"] = 0x08000000

        self.ffmpeg_proc = subprocess.Popen(cmd, **kwargs)
        threading.Thread(target=self._log_ffmpeg, daemon=True).start()

    def _stop_ffmpeg(self):
        if self.ffmpeg_proc and self.ffmpeg_proc.poll() is None:
            try:
                if os.name == "nt":
                    self.ffmpeg_proc.terminate()
                else:
                    os.killpg(os.getpgid(self.ffmpeg_proc.pid), signal.SIGTERM)
            except Exception as e:
                print(f"[FFmpeg] Stop error: {e}")
            try:
                self.ffmpeg_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.ffmpeg_proc.kill()
            self.ffmpeg_proc = None
            print("[FFmpeg] Process stopped.")

    def _log_ffmpeg(self):
        for line in self.ffmpeg_proc.stderr:
            print(f"[FFmpeg] {line.decode(errors='ignore').rstrip()}")

    def _probe_has_audio(self, filepath: str) -> bool:
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "a",
                 "-show_entries", "stream=codec_type",
                 "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=5
            )
            return "audio" in result.stdout
        except Exception:
            return False

    def _probe_dimensions(self, filepath: str):
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error",
                 "-select_streams", "v:0",
                 "-show_entries", "stream=width,height",
                 "-of", "csv=p=0", filepath],
                capture_output=True, text=True, timeout=5
            )
            parts = result.stdout.strip().split(",")
            if len(parts) == 2:
                return int(parts[0]), int(parts[1])
        except Exception:
            pass
        return 1280, 720

    def _build_sdp(self, has_audio: bool, width: int = 1280, height: int = 720) -> str:
        ip         = self.client_address[0]
        video_port = self.client_rtp_port or 5004
        audio_port = self._audio_port()

        sdp = (
            "v=0\r\n"
            f"o=- 0 0 IN IP4 {ip}\r\n"
            "s=RTSP Stream\r\n"
            f"c=IN IP4 {ip}\r\n"
            "t=0 0\r\n"
            f"a=x-dimensions:{width},{height}\r\n"
            f"m=video {video_port} RTP/AVP 96\r\n"
            "a=rtpmap:96 H264/90000\r\n"
            "a=control:track1\r\n"
        )
        if has_audio:
            sdp += (
                f"m=audio {audio_port} RTP/AVP 14\r\n"
                "b=AS:128\r\n"
                "a=rtpmap:14 MPA/90000\r\n"
                "a=control:track2\r\n"
            )
        return sdp

    def _extract_path(self, url: str) -> str:
        if url.startswith("rtsp://"):
            url = url[7:]
            if "/" in url:
                return url.split("/", 1)[1].split("/track")[0]
        return url.lstrip("/").split("/track")[0] or "stream.mp4"

    def _parse_client_port(self, transport: str) -> int:
        for part in transport.split(";"):
            if "client_port" in part:
                try:
                    ports = part.split("=")[1]
                    return int(ports.split("-")[0])
                except (IndexError, ValueError):
                    pass
        return 5004

    def send_response(self, code: int, cseq: str, headers: dict = None, body: str = ""):
        reasons = {
            200: "OK",
            400: "Bad Request",
            404: "Not Found",
            455: "Method Not Valid in This State",
            501: "Not Implemented",
        }
        reason = reasons.get(code, "Unknown")
        lines = [f"RTSP/1.0 {code} {reason}", f"CSeq: {cseq}"]
        if headers:
            for k, v in headers.items():
                lines.append(f"{k}: {v}")
        if body:
            lines.append(f"Content-Length: {len(body)}")
        response = CRLF.join(lines) + CRLF + CRLF + body
        print(f"[RTSP] << Response:\n{response.strip()}\n")
        try:
            self.client_socket.sendall(response.encode("utf-8"))
        except OSError as e:
            print(f"[RTSP] Send error: {e}")


class RtspServer:
    def __init__(self, host="", port=RTSP_PORT):
        self.host = host
        self.port = port

    def start(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind((self.host, self.port))
            srv.listen(10)
            print(f"[RTSP] Server listening on port {self.port}")
            print(f"       rtsp://127.0.0.1/your_video.mp4\n")
            try:
                while True:
                    client_sock, client_addr = srv.accept()
                    RTSPServerWorker(client_sock, client_addr).start()
            except KeyboardInterrupt:
                print("\n[RTSP] Server shutting down.")


if __name__ == "__main__":
    RtspServer().start()