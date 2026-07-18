"""
Shared wire protocol for the RTSP-style video/live server & client.

Every message sent over EITHER socket (main control/VOD socket, or the
live-upload socket) uses the same framing so binary frame data can never
get corrupted by mixing with text control messages on the same stream:

    [ 1 byte type ][ 4 byte big-endian length ][ payload ]

Types:
    TYPE_TEXT  -> payload is utf-8 text (commands, RTSP-style responses,
                  VIDEOLIST, LIVE_STARTED / LIVE_ENDED notifications)
    TYPE_VOD   -> payload is a JPEG-encoded video-file frame
    TYPE_LIVE  -> payload is a JPEG-encoded live-camera frame
"""

import struct

TYPE_TEXT = b'T'
TYPE_VOD  = b'V'
TYPE_LIVE = b'L'

HEADER_FMT  = ">cL"
HEADER_SIZE = struct.calcsize(HEADER_FMT)

MAX_PAYLOAD = 8_000_000  # sanity cap, guards against a corrupted stream


def pack_message(msg_type: bytes, payload: bytes) -> bytes:
    return struct.pack(HEADER_FMT, msg_type, len(payload)) + payload


def send_message(sock, msg_type: bytes, payload: bytes, lock=None):
    """Send one framed message. Pass `lock` if multiple threads write
    to the same socket (e.g. server's stream thread + command thread)."""
    packet = pack_message(msg_type, payload)
    if lock is not None:
        with lock:
            sock.sendall(packet)
    else:
        sock.sendall(packet)


def send_text(sock, text: str, lock=None):
    send_message(sock, TYPE_TEXT, text.encode(), lock)


# ── Live-stream framing ─────────────────────────────────────────
# Multiple clients can be live at the same time now, so every TYPE_LIVE
# payload that goes out to a *viewer* (main control socket) is tagged
# with the stream id of whichever client it came from:
#
#     [ 1 byte id length ][ id bytes (utf-8) ][ JPEG bytes ]
#
# Note: on the raw upload socket (client -> server, LIVE_PORT) frames
# are sent WITHOUT this wrapper (just raw JPEG bytes) since that socket
# only ever carries one broadcaster's frames; the server adds the
# wrapper when relaying to viewers on the shared main socket.

def pack_live_payload(stream_id: str, jpeg_bytes: bytes) -> bytes:
    sid = stream_id.encode()
    if len(sid) > 255:
        raise ValueError("stream_id too long to fit in 1-byte length prefix")
    return struct.pack(">B", len(sid)) + sid + jpeg_bytes


def unpack_live_payload(payload: bytes):
    """Returns (stream_id: str, jpeg_bytes: bytes)."""
    (sid_len,) = struct.unpack(">B", payload[:1])
    sid = payload[1:1 + sid_len].decode(errors="ignore")
    jpeg_bytes = payload[1 + sid_len:]
    return sid, jpeg_bytes


class FrameReceiver:
    """Feed it raw bytes as they arrive from recv(); it yields complete
    (msg_type, payload) messages, buffering partial ones across calls."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        self._buf += chunk

    def pop_messages(self):
        messages = []
        while True:
            if len(self._buf) < HEADER_SIZE:
                break
            msg_type, length = struct.unpack(HEADER_FMT, self._buf[:HEADER_SIZE])
            if length > MAX_PAYLOAD or length == 0:
                # Corrupted stream — can't safely resync, drop it.
                self._buf = b""
                break
            if len(self._buf) < HEADER_SIZE + length:
                break
            payload = self._buf[HEADER_SIZE:HEADER_SIZE + length]
            self._buf = self._buf[HEADER_SIZE + length:]
            messages.append((msg_type, payload))
        return messages
