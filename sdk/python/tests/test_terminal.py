"""Terminál (wbx connect): adresa WebSocketu a minimální klient WebSocketu proti lokálnímu serveru."""
import base64
import hashlib
import os
import socket
import struct
import sys
import threading
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

from wbxsandbox import terminal  # noqa: E402
from wbxsandbox.cli import main  # noqa: E402


def _server(handler):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def run():
        c, _ = srv.accept()
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        handler(c, req.decode())
        c.close()
        srv.close()

    threading.Thread(target=run, daemon=True).start()
    return srv.getsockname()[1]


def _frame(opcode, payload):
    return bytes([0x80 | opcode, len(payload)]) + payload


def _read_client_frame(c):
    b0, b1 = c.recv(2)
    n = b1 & 0x7F
    if n == 126:
        n = struct.unpack("!H", c.recv(2))[0]
    mask = c.recv(4)
    data = b""
    while len(data) < n:
        data += c.recv(n - len(data))
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


class TerminalTest(unittest.TestCase):
    def test_url(self):
        self.assertEqual(terminal.terminal_url("https://sandbox.wbx.cz/", "sb_n01_x", 80, 24),
                         "wss://sandbox.wbx.cz/v1/sandboxes/sb_n01_x/terminal?cols=80&rows=24")
        self.assertEqual(terminal.terminal_url("http://10.0.0.1:3000", "sb_n01_x", 1, 2),
                         "ws://10.0.0.1:3000/v1/sandboxes/sb_n01_x/terminal?cols=1&rows=2")

    def test_websocket_roundtrip(self):
        got = {}

        def handler(c, req):
            got["req"] = req
            key = [l.split(": ", 1)[1] for l in req.split("\r\n") if l.lower().startswith("sec-websocket-key")][0]
            acc = base64.b64encode(hashlib.sha1((key + terminal._GUID).encode()).digest()).decode()
            c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode())
            c.sendall(_frame(0x9, b"hi"))  # ping → klient odpoví pong
            op, data = _read_client_frame(c)
            got["pong"] = (op, data)
            c.sendall(bytes([0x01, 3]) + b'{"a' + _frame(0x0, b'":1}'))  # fragmentovaná zpráva
            op, data = _read_client_frame(c)
            got["sent"] = (op, data)
            c.sendall(_frame(0x8, struct.pack("!H", 1000)))
            _read_client_frame(c)

        port = _server(handler)
        ws = terminal.WebSocket(f"ws://127.0.0.1:{port}/v1/sandboxes/sb/terminal?cols=1", {"X-API-Key": "k"})
        self.assertEqual(ws.recv_text(), '{"a":1}')
        ws.send_text("x" * 300)  # délka přes 126 bajtů
        self.assertIsNone(ws.recv_text())
        ws.close()
        self.assertIn("GET /v1/sandboxes/sb/terminal?cols=1 HTTP/1.1", got["req"])
        self.assertIn("X-API-Key: k", got["req"])
        self.assertEqual(got["pong"], (0xA, b"hi"))
        self.assertEqual(got["sent"], (0x1, b"x" * 300))

    def test_http_error_is_reported(self):
        def handler(c, req):
            c.sendall(b'HTTP/1.1 404 Not Found\r\nContent-Type: application/json\r\n\r\n{"error":"sandbox not found"}')

        port = _server(handler)
        with self.assertRaises(terminal.WebSocketError) as e:
            terminal.WebSocket(f"ws://127.0.0.1:{port}/x", {})
        self.assertIn("sandbox not found", str(e.exception))

    def test_cli_requires_key(self):
        env = os.environ.pop("WBXSANDBOX_API_KEY", None)
        try:
            self.assertEqual(main(["connect", "sb_x", "--url", "http://127.0.0.1:1"]), 2)
        finally:
            if env is not None:
                os.environ["WBXSANDBOX_API_KEY"] = env


if __name__ == "__main__":
    unittest.main()
