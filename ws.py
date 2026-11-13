"""Tiny WebSocket client (RFC 6455) using only the standard library.

Used to get *instant* push notifications from a Solana RPC (logsSubscribe) so a wallet is
re-checked the moment it transacts instead of waiting for the next poll."""
import base64
import hashlib
import os
import socket
import ssl
import struct
from urllib.parse import urlparse

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WSError(Exception):
    pass


class WSClient:
    def __init__(self, url, timeout=10):
        self.url, self.timeout = url, timeout
        self.sock = None
        self.buf = b""
        self._frag = None
        self.bytes_in = 0

    def connect(self):
        u = urlparse(self.url)
        if u.scheme not in ("ws", "wss") or not u.hostname:
            raise WSError("Bad websocket URL")
        port = u.port or (443 if u.scheme == "wss" else 80)
        try:
            raw = socket.create_connection((u.hostname, port), timeout=self.timeout)
            if u.scheme == "wss":
                raw = ssl.create_default_context().wrap_socket(raw, server_hostname=u.hostname)
        except (OSError, ssl.SSLError) as e:
            raise WSError(f"connect failed: {e}") from e
        self.sock = raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host = u.hostname + (f":{u.port}" if u.port else "")
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        try:
            self.sock.sendall(req.encode())
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise WSError("closed during handshake")
                head += chunk
                if len(head) > 65536:
                    raise WSError("handshake too large")
        except (OSError, ssl.SSLError) as e:
            self.close()
            raise WSError(f"handshake failed: {e}") from e
        header, _, rest = head.partition(b"\r\n\r\n")
        lines = header.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            self.close()
            raise WSError("server refused websocket: " + lines[0][:80])
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        got = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        if got.get("sec-websocket-accept") != accept:
            self.close()
            raise WSError("bad websocket handshake")
        self.buf = rest
        self.sock.settimeout(self.timeout)

    def _send(self, opcode, payload):
        if not self.sock:
            raise WSError("not connected")
        mask = os.urandom(4)
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack(">Q", n)
        body = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        try:
            self.sock.sendall(head + mask + body)
        except (OSError, ssl.SSLError) as e:
            raise WSError(f"send failed: {e}") from e

    def send_text(self, text):
        self._send(0x1, text.encode("utf-8"))

    def _parse_frame(self):
        b = self.buf
        if len(b) < 2:
            return None
        fin, opcode = bool(b[0] & 0x80), b[0] & 0x0F
        masked, n, pos = bool(b[1] & 0x80), b[1] & 0x7F, 2
        if n == 126:
            if len(b) < 4:
                return None
            n, pos = struct.unpack(">H", b[2:4])[0], 4
        elif n == 127:
            if len(b) < 10:
                return None
            n, pos = struct.unpack(">Q", b[2:10])[0], 10
        if n > 16 * 1024 * 1024:
            raise WSError("frame too large")
        if masked:
            if len(b) < pos + 4:
                return None
            mk, pos = b[pos:pos + 4], pos + 4
        if len(b) < pos + n:
            return None
        payload = b[pos:pos + n]
        if masked:
            payload = bytes(c ^ mk[i % 4] for i, c in enumerate(payload))
        self.buf = b[pos + n:]
        return fin, opcode, payload

    def recv(self, timeout=None):
        """Next complete text message, or None if nothing arrived within `timeout` seconds."""
        if not self.sock:
            raise WSError("not connected")
        self.sock.settimeout(timeout if timeout is not None else self.timeout)
        while True:
            frame = self._parse_frame()
            if frame is None:
                try:
                    chunk = self.sock.recv(65536)
                except socket.timeout:
                    return None
                except (OSError, ssl.SSLError) as e:
                    if "timed out" in str(e):
                        return None
                    raise WSError(f"recv failed: {e}") from e
                if not chunk:
                    raise WSError("connection closed")
                self.bytes_in += len(chunk)
                self.buf += chunk
                continue
            fin, op, payload = frame
            if op == 0x9:      # ping -> pong
                self._send(0xA, payload)
            elif op == 0x8:    # close
                self.close()
                raise WSError("server closed the connection")
            elif op in (0x1, 0x2, 0x0):
                if op != 0x0:
                    self._frag = bytearray()
                if self._frag is None:
                    continue
                self._frag += payload
                if fin:
                    msg, self._frag = bytes(self._frag), None
                    return msg.decode("utf-8", "replace")

    def ping(self):
        self._send(0x9, b"")

    def close(self):
        s, self.sock = self.sock, None
        if s:
            try:
                s.close()
            except OSError:
                pass
