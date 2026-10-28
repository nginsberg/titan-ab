"""Thin client: one newline-delimited JSON request/response over the daemon's unix socket. Never imports bleak."""
from __future__ import annotations

import json
import socket
from pathlib import Path


class DaemonDown(Exception):
    pass


def send(runs_dir, req: dict, timeout: float = 30.0) -> dict:
    path = str(Path(runs_dir) / "titan.sock")
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        try:
            s.connect(path)
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise DaemonDown("daemon is not running (%s). The owner starts it in Terminal: ./start-daemon.command"
                             % type(e).__name__) from None
        s.sendall((json.dumps(req) + "\n").encode())
        buf = b""
        while not buf.endswith(b"\n"):
            part = s.recv(65536)
            if not part:
                break
            buf += part
    finally:
        s.close()
    if not buf.strip():
        raise DaemonDown("daemon closed the connection without a reply")
    return json.loads(buf.decode())
