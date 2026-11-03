from __future__ import annotations
import json
import os
import pytest
from bili_asr import audio
from bili_asr import bili_client as bc
from bili_asr.cli.main import main
from bili_asr.manifest import ManifestStore


SPI_OK = (200, {"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})


IMG_KEY = "7cd084941338484aae1ad9425b84077c"


SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


STREAM_HOST = "upos-sz-mirrorcoso.bilivideo.com"


class RouterTransport:
    """Routes by URL substring to scripted (status, body) queues.

    Also implements the binary stream seam used by Task 3: a
    `stream_bytes` route receives (url, headers) and returns bytes.
    """

    def __init__(self, routes: dict[str, list], stream_routes: dict[str, bytes] | None = None):
        self.routes = routes
        self.stream_routes = stream_routes or {}
        self.calls: list[dict] = []
        self.stream_calls: list[dict] = []

    def get_json(self, url, params=None, headers=None, cookies=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "headers": dict(headers or {}),
                           "cookies": dict(cookies or {})})
        for frag, queue in self.routes.items():
            if frag in url:
                if not queue:
                    raise AssertionError(f"queue for {frag!r} exhausted")
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"no route for {url}")

    def get_stream(self, url, headers=None, cookies=None, timeout=None) -> bytes:
        self.stream_calls.append({"url": url, "headers": dict(headers or {}),
                                  "cookies": dict(cookies or {})})
        for frag, payload in self.stream_routes.items():
            if frag in url:
                return payload
        raise AssertionError(f"no stream route for {url}")


class FastSleeper:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def playurl_ok(streams=None, code=30216):
    """dash playurl with selectable audio codec ids (default DTS m4s)."""
    audio_list = streams if streams is not None else [
        {"id": 30216, "baseUrl": f"https://{STREAM_HOST}/a30216.m4s",
         "base_url": f"https://{STREAM_HOST}/a30216.m4s",
         "backupUrl": [], "bandwidth": 320000},
        {"id": 30280, "baseUrl": f"https://{STREAM_HOST}/a30280.m4s",
         "base_url": f"https://{STREAM_HOST}/a30280.m4s",
         "backupUrl": [], "bandwidth": 192000},
    ]
    return (200, {"code": 0, "data": {"dash": {"audio": audio_list}}})


def nav_response(img_key=IMG_KEY, sub_key=SUB_KEY):
    return (
        200,
        {"code": -101, "data": {"wbi_img": {
            "img_url": f"https://i0.hdslb.com/bfs/wbi/{img_key}.png",
            "sub_url": f"https://i0.hdslb.com/bfs/wbi/{sub_key}.png",
        }}},
    )


def make_client(routes, stream_routes=None):
    routes.setdefault("finger/spi", [SPI_OK])
    routes.setdefault("nav", [nav_response()])
    return bc.BiliClient(
        transport=RouterTransport(routes, stream_routes),
        sleeper=FastSleeper(),
        jitter=lambda: 0.0,
    )


AUDIO_BYTES = b"\x00\x00\x00\x18ftypM4A " + b"payload" * 100
