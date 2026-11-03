"""The optional relay is destination restricted and forwards bytes opaquely."""

import importlib.util
from pathlib import Path
import socket
import threading
from types import SimpleNamespace

import pytest


pytest.importorskip("paramiko")
spec = importlib.util.spec_from_file_location("opencode_relay", Path(__file__).resolve().parents[1] / "scripts/deployment/opencode_ssh_relay.py")
relay = importlib.util.module_from_spec(spec)
spec.loader.exec_module(relay)


@pytest.mark.parametrize("payload", [b"CONNECT example.com:443 HTTP/1.1", b"CONNECT opencode.ai:80 HTTP/1.1", b"GET / HTTP/1.1"])
def test_rejects_other_destinations_without_opening_connection(monkeypatch, payload):
    opened = []
    monkeypatch.setattr(relay.socket, "create_connection", lambda *_args, **_kwargs: opened.append(True))
    caller, channel = socket.socketpair()
    caller.settimeout(2)
    worker = threading.Thread(target=relay.serve, args=(channel,))
    worker.start()
    try:
        caller.sendall(payload + b"\r\nHost: opencode.ai\r\n\r\n")
        assert caller.recv(4096).startswith(b"HTTP/1.1 403")
        assert opened == []
    finally:
        caller.close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def test_connect_forwards_opaque_tls_bytes_and_closes(monkeypatch):
    caller, channel = socket.socketpair()
    upstream, provider = socket.socketpair()
    caller.settimeout(2)
    provider.settimeout(2)
    destinations = []
    def connect(destination, **_kwargs):
        destinations.append(destination)
        return upstream
    monkeypatch.setattr(relay.socket, "create_connection", connect)
    worker = threading.Thread(target=relay.serve, args=(channel,))
    worker.start()
    try:
        caller.sendall(b"CONNECT opencode.ai:443 HTTP/1.0\r\n\r\n")
        assert caller.recv(4096).startswith(b"HTTP/1.1 200")
        caller.sendall(b"\x16\x03\x01opaque-client-record")
        assert provider.recv(4096) == b"\x16\x03\x01opaque-client-record"
        provider.sendall(b"\x17\x03\x03opaque-provider-record")
        assert caller.recv(4096) == b"\x17\x03\x03opaque-provider-record"
        assert destinations == [("opencode.ai", 443)]
    finally:
        caller.close()
        provider.close()
        worker.join(timeout=3)
        assert not worker.is_alive()


def test_stop_cancels_listener_and_drains_channels_before_closing_ssh(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    finished = threading.Event()
    states = []
    channel = object()

    def serve(_channel):
        assert _channel is channel
        assert finished.wait(2)

    def accept(**_):
        stop.touch()
        return channel

    def cancel(host, port):
        assert (host, port) == ("127.0.0.1", 18443)
        assert states[-1] == (False, "draining")
        finished.set()

    closed = []
    transport = SimpleNamespace(
        request_port_forward=lambda *_: None, is_active=lambda: True,
        accept=accept, cancel_port_forward=cancel,
        close=lambda: closed.append(finished.is_set()),
    )
    monkeypatch.setattr(relay, "serve", serve)
    relay.forward_channels(transport, 18443, stop, lambda *values: states.append(values))
    assert states == [(True, "forwarding"), (False, "draining")]
    assert closed == [True]


def test_excess_channels_are_closed_without_unbounded_queue(tmp_path, monkeypatch):
    stop = tmp_path / "STOP"
    finish = threading.Event()
    channels = [SimpleNamespace(closed=False) for _ in range(17)]
    for channel in channels:
        channel.close = lambda channel=channel: setattr(channel, "closed", True)
    accepted = iter(channels)
    served = []

    def serve(channel):
        assert finish.wait(2)
        served.append(channel)

    def accept(**_):
        channel = next(accepted)
        if channel is channels[-1]:
            stop.touch()
        return channel

    transport = SimpleNamespace(
        request_port_forward=lambda *_: None, is_active=lambda: True, accept=accept,
        cancel_port_forward=lambda *_: finish.set(), close=lambda: None,
    )
    monkeypatch.setattr(relay, "serve", serve)
    relay.forward_channels(transport, 18443, stop, lambda *_: None)
    assert len(served) == 16
    assert channels[-1].closed


def test_unexpected_host_key_never_sends_password(tmp_path, monkeypatch):
    authenticated = []
    stop = tmp_path / "STOP"
    transport = SimpleNamespace(
        start_client=lambda **_: None,
        get_remote_server_key=lambda: SimpleNamespace(get_fingerprint=lambda: b"wrong"),
        auth_password=lambda *_: authenticated.append(True),
        close=lambda: stop.touch(),
    )
    monkeypatch.setattr(relay.socket, "create_connection", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(relay.paramiko, "Transport", lambda _: transport)
    args = SimpleNamespace(state_dir=tmp_path, host="ssh.example", port=22,
                           fingerprint="expected", user="operator", remote_port=18443)
    relay.run(args, "test-password")
    assert authenticated == []
    assert not (tmp_path / "relay.pid").exists()
    assert '"reason": "stopped"' in (tmp_path / "state.json").read_text()
