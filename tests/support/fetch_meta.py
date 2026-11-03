from __future__ import annotations
import pytest
from bili_asr import bili_client as bc
from bili_asr.cli.parser import build_parser
import urllib.error
from bili_asr.cli.parser import build_parser


SPI_OK = (200, {"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})


SPI_NEW = (200, {"code": 0, "data": {"b_3": "B3NEW", "b_4": "B4NEW"}})


class FakeTransport:
    """Scripted injectable transport; records calls, never opens sockets."""

    def __init__(self, script, spi=None):
        # script: (status, body) responses for endpoint GETs (in order)
        # spi: finger/spi responses; refreshes draw from the same queue
        self.script = list(script)
        self.spi = list(spi or [SPI_OK])
        self.calls = []

    def get_json(self, url, params=None, headers=None, cookies=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "cookies": dict(cookies or {}),
            }
        )
        if "pagelist" in url:
            cid = abs(hash((params or {}).get("bvid") or "x")) % 10_000 + 1
            return 200, {
                "code": 0,
                "data": [{"cid": cid, "page": 1, "part": ""}],
            }
        queue = self.spi if "finger/spi" in url else self.script
        if not queue:
            raise AssertionError("FakeTransport ran out of scripted responses")
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return status, body


def ok_page(archives, total=None):
    return {
        "code": 0,
        "message": "0",
        "data": {
            "page": {"total": total if total is not None else len(archives)},
            "archives": archives,
        },
    }


def arc(bvid, duration=100, pubdate=1700000000, title=None):
    return {
        "bvid": bvid,
        "aid": 1,
        "title": title if title is not None else "t " + bvid,
        "duration": duration,
        "pubdate": pubdate,
        "stat": {"view": 10},
        "state": 0,
    }


class FastSleeper:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)
