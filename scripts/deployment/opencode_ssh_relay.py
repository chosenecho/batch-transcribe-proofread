"""Forward a server loopback CONNECT proxy through SSH to this machine's egress.

Only opencode.ai:443 is accepted. TLS remains between requests and OpenCode.
The SSH password is read interactively and stays in memory, including daemon mode.
"""

import argparse
import concurrent.futures
import getpass
import json
import os
from pathlib import Path
import select
import socket
import subprocess
import sys
import threading
import time

import paramiko


def serve(channel):
    upstream = None
    try:
        channel.settimeout(10)
        header = b""
        while b"\r\n\r\n" not in header:
            data = channel.recv(1024)
            if not data:
                return
            header += data
            if len(header) > 8192:
                return
        first_line = header.split(b"\r\n", 1)[0]
        if first_line != b"CONNECT opencode.ai:443 HTTP/1.1" and first_line != b"CONNECT opencode.ai:443 HTTP/1.0":
            channel.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        upstream = socket.create_connection(("opencode.ai", 443), timeout=10)
        channel.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        remaining = header.split(b"\r\n\r\n", 1)[1]
        if remaining:
            upstream.sendall(remaining)
        upstream.settimeout(30)
        channel.settimeout(30)
        last_data = time.monotonic()
        while time.monotonic() - last_data < 1900:
            readable, _, _ = select.select([channel, upstream], [], [], 2)
            for source in readable:
                data = source.recv(65536)
                if not data:
                    return
                (upstream if source is channel else channel).sendall(data)
                last_data = time.monotonic()
    except (OSError, EOFError, paramiko.SSHException):
        pass
    finally:
        channel.close()
        if upstream is not None:
            upstream.close()


def forward_channels(transport, remote_port, stop, status):
    """Bound accepted channels and drain them before closing the SSH connection."""
    slots = threading.BoundedSemaphore(16)
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        transport.request_port_forward("127.0.0.1", remote_port)
        status(True, "forwarding")
        last_status = time.monotonic()
        while transport.is_active() and not stop.exists():
            channel = transport.accept(timeout=1)
            if channel is not None:
                if slots.acquire(blocking=False):
                    try:
                        future = pool.submit(serve, channel)
                    except Exception:
                        slots.release()
                        channel.close()
                        raise
                    future.add_done_callback(lambda _future: slots.release())
                else:
                    channel.close()
            if time.monotonic() - last_status > 20:
                status(True, "forwarding")
                last_status = time.monotonic()
        status(False, "draining" if stop.exists() else "ssh_disconnected")
        if transport.is_active():
            transport.cancel_port_forward("127.0.0.1", remote_port)
    # The executor has finished all active channels while SSH was still open.
    transport.close()


def run(args, password):
    args.state_dir.mkdir(parents=True, exist_ok=True)
    stop = args.state_dir / "STOP"
    state = args.state_dir / "state.json"
    pidfile = args.state_dir / "relay.pid"
    pidfile.write_text(str(os.getpid()))
    def status(ready, reason):
        temporary = state.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid(), "ready": ready, "reason": reason,
            "updated_at_epoch": time.time(), "remote_bind": f"127.0.0.1:{args.remote_port}",
            "allowed_destination": "opencode.ai:443"}))
        temporary.replace(state)
    try:
        while not stop.exists():
            # A fingerprint is checked before password authentication below.
            transport = None
            try:
                connection = socket.create_connection((args.host, args.port), timeout=15)
                transport = paramiko.Transport(connection)
                transport.start_client(timeout=15)
                if transport.get_remote_server_key().get_fingerprint().hex() != args.fingerprint:
                    raise RuntimeError("unexpected SSH host fingerprint")
                transport.auth_password(args.user, password)
                transport.set_keepalive(20)
                forward_channels(transport, args.remote_port, stop, status)
            except Exception as exc:
                status(False, type(exc).__name__)
                if transport is not None:
                    transport.close()
                for _ in range(10):
                    if stop.exists():
                        break
                    time.sleep(1)
        status(False, "stopped")
    finally:
        pidfile.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--user", default="root")
    parser.add_argument("--fingerprint", required=True)
    parser.add_argument("--remote-port", type=int, default=18443)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--daemon", action="store_true")
    parser.add_argument("--password-stdin", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    password = sys.stdin.readline().rstrip("\r\n") if args.password_stdin else getpass.getpass("SSH password: ")
    args.state_dir.mkdir(parents=True, exist_ok=True)
    if args.daemon:
        pidfile = args.state_dir / "relay.pid"
        if pidfile.exists():
            pid = int(pidfile.read_text())
            # Windows os.kill(pid, 0) can terminate a process; use the process API.
            if os.name == "nt":
                import ctypes
                handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
                if handle:
                    ctypes.windll.kernel32.CloseHandle(handle)
                    raise SystemExit("relay process already exists")
            else:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    raise SystemExit("relay process already exists")
        (args.state_dir / "STOP").unlink(missing_ok=True)
        command = [sys.executable, str(Path(__file__).resolve())] + [v for v in sys.argv[1:] if v != "--daemon"] + ["--password-stdin"]
        with (args.state_dir / "relay.log").open("a", encoding="utf-8") as log:
            child = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                start_new_session=os.name != "nt")
            child.stdin.write((password + "\n").encode())
            child.stdin.close()
        print(json.dumps({"pid": child.pid, "state_dir": str(args.state_dir)}))
    else:
        run(args, password)


if __name__ == "__main__":
    main()
