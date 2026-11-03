from __future__ import annotations
import json
import os
from bili_asr import bili_client as bc
from bili_asr import subtitles
from bili_asr.manifest import ManifestStore


SPI_OK = (200, {"code": 0, "data": {"b_3": "B3", "b_4": "B4"}})


IMG_KEY = "7cd084941338484aae1ad9425b84077c"


SUB_KEY = "4932caff0ff746eab6f01bf08b70ac45"


SUBTITLE_URL = "//aisubtitle.hdslb.com/bfs/ai_subtitle/prod/SHORTLIVED.json"


class RouterTransport:
    """Routes by URL substring to scripted (status, body) queues."""

    def __init__(self, routes: dict[str, list]):
        self.routes = routes
        self.calls: list[dict] = []

    def get_json(self, url, params=None, headers=None, cookies=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "params": dict(params or {}),
                "headers": dict(headers or {}),
                "cookies": dict(cookies or {}),
            }
        )
        for frag, queue in self.routes.items():
            if frag in url:
                if not queue:
                    raise AssertionError(f"queue for {frag!r} exhausted")
                item = queue.pop(0)
                if isinstance(item, Exception):
                    raise item
                return item
        raise AssertionError(f"no route for {url}")


class FastSleeper:
    def __init__(self):
        self.waits = []

    def __call__(self, seconds):
        self.waits.append(seconds)


def nav_ok():
    return (
        200,
        {
            "code": -101,  # spike: wbi_img populated even when not logged in
            "data": {
                "wbi_img": {
                    "img_url": f"https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png",
                    "sub_url": f"https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png",
                }
            },
        },
    )


def player_ok(subtitles_list, extra_data=None):
    data = {
        "subtitle": {"subtitles": subtitles_list, "allow_submit": False},
        "need_login_subtitle": not subtitles_list,
    }
    if extra_data:
        data.update(extra_data)
    return (200, {"code": 0, "data": data})


def sub_entry(lan="ai-zh", lan_doc="中文（自动生成）", url=SUBTITLE_URL):
    return {"lan": lan, "lan_doc": lan_doc, "subtitle_url": url,
            "ai_type": 1, "ai_status": 2}


def make_client(routes, sessdata=None):
    routes.setdefault("finger/spi", [SPI_OK])
    return bc.BiliClient(
        transport=RouterTransport(routes),
        sleeper=FastSleeper(),
        jitter=lambda: 0.0,
        sessdata=sessdata,
    )


SAMPLE_DOC = {
    "body": [
        {"from": 1.0, "to": 2.5, "content": "你好"},
        {"from": 2.5, "to": 4.0, "content": "世界"},
        {"from": 61.25, "to": 62.0, "content": "第三句"},
    ]
}
