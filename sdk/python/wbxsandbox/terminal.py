"""Interactive terminal in a running sandbox (`wbx connect <sandbox-id>`).

The terminal runs over a WebSocket on the same address and port as the REST API
(GET /v1/sandboxes/{id}/terminal), so nothing else has to be reachable. Only the
Python standard library is used – a minimal WebSocket client is included below.
Behind an HTTP proxy, set HTTPS_PROXY (NO_PROXY is respected); the connection is
tunnelled with CONNECT.

    wbx connect sb_n01_0123456789abcdef

Type `exit` to end the shell, or press Ctrl-] to disconnect. Closing the terminal
ends the shell; the sandbox keeps running until its timeout.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import socket
import ssl
import struct
import sys
import threading
import urllib.parse
import urllib.request
from typing import Callable, Optional, Tuple

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_DETACH = b"\x1d"  # Ctrl-]


class WebSocketError(Exception):
    pass


class WebSocket:
    """Minimal RFC 6455 client: text frames, ping/pong, close. Not for general use."""

    def __init__(self, url: str, headers: Optional[dict] = None, timeout: float = 30):
        u = urllib.parse.urlsplit(url)
        if u.scheme not in ("ws", "wss"):
            raise WebSocketError(f"unsupported URL scheme: {u.scheme}")
        host = u.hostname or ""
        port = u.port or (443 if u.scheme == "wss" else 80)
        proxy = _proxy_for(u.scheme, host)
        if proxy:
            raw = _connect_via_proxy(proxy, host, port, timeout)
        else:
            raw = socket.create_connection((host, port), timeout=timeout)
        if u.scheme == "wss":
            raw = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
        self.sock = raw
        self._wlock = threading.Lock()
        self._buf = b""
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        host_hdr = host if u.port is None else f"{host}:{port}"
        lines = [f"GET {path} HTTP/1.1", f"Host: {host_hdr}", "Upgrade: websocket", "Connection: Upgrade",
                 f"Sec-WebSocket-Key: {key}", "Sec-WebSocket-Version: 13"]
        lines += [f"{k}: {v}" for k, v in (headers or {}).items()]
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        head = self._read_until(b"\r\n\r\n", 65536)
        status_line, _, rest = head.partition(b"\r\n")
        parts = status_line.split(b" ", 2)
        code = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
        if code != 101:
            body = self._buf[:4096].decode(errors="replace")
            try:
                body = json.loads(body).get("error", body)
            except ValueError:
                pass
            raise WebSocketError(f"HTTP {code}: {body.strip() or status_line.decode(errors='replace')}")
        hdrs = {}
        for line in rest.split(b"\r\n"):
            k, _, v = line.partition(b":")
            hdrs[k.strip().lower()] = v.strip()
        want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest())
        if hdrs.get(b"sec-websocket-accept") != want:
            raise WebSocketError("invalid WebSocket handshake from the server")
        self.sock.settimeout(None)

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self._buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            self._buf += chunk
            if len(self._buf) > limit:
                raise WebSocketError("handshake response too large")
        head, _, self._buf = self._buf.partition(marker)
        return head

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise WebSocketError("connection closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        head = bytes([0x80 | opcode])
        n = len(payload)
        if n < 126:
            head += bytes([0x80 | n])
        elif n < 65536:
            head += bytes([0x80 | 126]) + struct.pack("!H", n)
        else:
            head += bytes([0x80 | 127]) + struct.pack("!Q", n)
        mask = os.urandom(4)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        with self._wlock:
            self.sock.sendall(head + mask + masked)

    def send_text(self, text: str) -> None:
        self._send_frame(0x1, text.encode())

    def recv_text(self) -> Optional[str]:
        """Next text message, or None when the connection was closed."""
        data = b""
        while True:
            b0, b1 = self._read_exact(2)
            fin, opcode = b0 & 0x80, b0 & 0x0F
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack("!H", self._read_exact(2))[0]
            elif n == 127:
                n = struct.unpack("!Q", self._read_exact(8))[0]
            if n > 16 << 20:
                raise WebSocketError("message too large")
            mask = self._read_exact(4) if b1 & 0x80 else b""
            payload = self._read_exact(n)
            if mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x8:  # close
                try:
                    self._send_frame(0x8, payload[:2])
                except OSError:
                    pass
                return None
            if opcode == 0x9:  # ping
                self._send_frame(0xA, payload)
                continue
            if opcode == 0xA:
                continue
            data += payload
            if fin:
                return data.decode(errors="replace")

    def close(self) -> None:
        try:
            self._send_frame(0x8, struct.pack("!H", 1000))
        except OSError:
            pass
        try:
            self.sock.close()
        except OSError:
            pass


def _proxy_for(scheme: str, host: str) -> Optional[str]:
    """Proxy from HTTPS_PROXY / HTTP_PROXY (wss / ws), unless NO_PROXY excludes the host."""
    try:
        if urllib.request.proxy_bypass(host):
            return None
    except OSError:
        pass
    return urllib.request.getproxies().get("https" if scheme == "wss" else "http") or None


def _connect_via_proxy(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """TCP tunnel through an HTTP proxy (CONNECT host:port), optionally with basic auth."""
    p = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if p.scheme not in ("http", ""):
        raise WebSocketError(f"unsupported proxy scheme: {p.scheme} (use an http:// proxy)")
    sock = socket.create_connection((p.hostname or "", p.port or 8080), timeout=timeout)
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    lines = [f"CONNECT {target} HTTP/1.1", f"Host: {target}"]
    if p.username is not None:
        cred = f"{urllib.parse.unquote(p.username)}:{urllib.parse.unquote(p.password or '')}"
        lines.append("Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode())
    try:
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise WebSocketError("the proxy closed the connection")
            buf += chunk
            if len(buf) > 65536:
                raise WebSocketError("proxy response too large")
        first = buf.split(b"\r\n", 1)[0]
        status = first.split(b" ", 2)
        if len(status) < 2 or status[1] != b"200":
            raise WebSocketError(f"the proxy refused the connection: {first.decode(errors='replace')}")
        if buf.index(b"\r\n\r\n") + 4 != len(buf):
            raise WebSocketError("unexpected data from the proxy")
    except BaseException:
        sock.close()
        raise
    return sock


def terminal_url(api_url: str, sandbox_id: str, cols: int, rows: int) -> str:
    u = urllib.parse.urlsplit(api_url.rstrip("/"))
    scheme = {"https": "wss", "http": "ws"}.get(u.scheme, u.scheme)
    path = u.path + "/v1/sandboxes/" + urllib.parse.quote(sandbox_id, safe="") + "/terminal"
    return urllib.parse.urlunsplit((scheme, u.netloc, path, urllib.parse.urlencode({"cols": cols, "rows": rows}), ""))


def _size() -> Tuple[int, int]:
    s = shutil.get_terminal_size((80, 24))
    return s.columns, s.lines


class _RawInput:
    """Keyboard in raw mode (every key goes to the sandbox, including Ctrl-C)."""

    def __init__(self):
        self.win = os.name == "nt"
        self.fd = None
        self.saved = None
        self.win_saved = []
        self.q = None

    def __enter__(self):
        if self.win:
            # Windows 10+: console in raw mode with VT input (arrows and keys arrive as escape
            # sequences, like on Linux) and VT output; reading blocks, so it runs in a thread
            import ctypes
            import queue
            k = ctypes.windll.kernel32
            k.SetConsoleCP(65001)
            k.SetConsoleOutputCP(65001)
            for handle, clear, add in ((-10, 0x0001 | 0x0002 | 0x0004, 0x0200), (-11, 0, 0x0004)):
                h = k.GetStdHandle(handle)
                mode = ctypes.c_uint32()
                if k.GetConsoleMode(h, ctypes.byref(mode)):
                    self.win_saved.append((h, mode.value))
                    k.SetConsoleMode(h, (mode.value & ~clear) | add)
            self.q = queue.Queue()

            def pump():
                while True:
                    try:
                        data = os.read(sys.stdin.fileno(), 4096)
                    except OSError:
                        data = b""
                    self.q.put(data or None)
                    if not data:
                        return

            threading.Thread(target=pump, daemon=True).start()
        elif sys.stdin.isatty():
            import termios
            import tty
            self.fd = sys.stdin.fileno()
            self.saved = termios.tcgetattr(self.fd)
            tty.setraw(self.fd)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            import termios
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        if self.win_saved:
            import ctypes
            for h, mode in self.win_saved:
                ctypes.windll.kernel32.SetConsoleMode(h, mode)

    def read(self, timeout: float) -> Optional[bytes]:
        """Pending keyboard input (b"" = nothing yet, None = end of input)."""
        if self.win:
            import queue
            try:
                return self.q.get(timeout=timeout)
            except queue.Empty:
                return b""
        import select
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        if not r:
            return b""
        data = os.read(sys.stdin.fileno(), 4096)
        return data if data else None


def connect(sandbox_id: str, url: Optional[str] = None, api_key: Optional[str] = None,
            out: Callable[[str], None] = None) -> int:
    """Open an interactive shell in the sandbox. Returns the shell's exit code (or 1)."""
    url = (url or os.environ.get("WBXSANDBOX_URL", "https://sandbox.wbx.cz")).rstrip("/")
    api_key = api_key or os.environ.get("WBXSANDBOX_API_KEY", "")
    if not api_key:
        sys.stderr.write("Set WBXSANDBOX_API_KEY (or pass --key).\n")
        return 2
    write = out or (lambda s: (sys.stdout.write(s), sys.stdout.flush()))
    cols, rows = _size()
    try:
        ws = WebSocket(terminal_url(url, sandbox_id, cols, rows), {"X-API-Key": api_key})
    except (OSError, WebSocketError) as e:
        sys.stderr.write(f"Cannot open the terminal: {e}\n")
        return 1

    state = {"done": False, "code": 1, "msg": ""}

    def reader():
        try:
            while True:
                text = ws.recv_text()
                if text is None:
                    break
                try:
                    m = json.loads(text)
                except ValueError:
                    continue
                t = m.get("type")
                if t == "output":
                    write(m.get("data", ""))
                elif t == "exit":
                    state["code"] = int(m.get("exit_code", 1))
                    break
                elif t == "error":
                    state["msg"] = m.get("message", "")
                    break
        except (OSError, WebSocketError) as e:
            if not state["done"]:
                state["msg"] = str(e)
        finally:
            state["done"] = True

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    last = (cols, rows)
    with _RawInput() as kb:
        while not state["done"]:
            data = kb.read(0.2)
            if data is None:
                break
            size = _size()
            if size != last:
                last = size
                try:
                    ws.send_text(json.dumps({"type": "resize", "cols": size[0], "rows": size[1]}))
                except OSError:
                    break
            if not data:
                continue
            if _DETACH in data:
                state["msg"] = "disconnected"
                break
            try:
                ws.send_text(json.dumps({"type": "input", "data_b64": base64.b64encode(data).decode()}))
            except OSError:
                break
    state["done"] = True
    ws.close()
    th.join(timeout=2)
    if state["msg"]:
        sys.stderr.write(f"\r\n[{state['msg']}]\r\n")
    return state["code"]
