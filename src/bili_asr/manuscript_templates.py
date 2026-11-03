"""Explicit historical renderer registry, composed above immutable implementations."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TypeVar

from bili_asr.manuscript_renderers_v1 import render_ai_v1, render_publish_v1
from bili_asr.manuscript_renderers_v2 import render_ai_v2, render_publish_v2

AI_RENDERERS = {"ai-draft-v1": render_ai_v1, "ai-draft-v2": render_ai_v2}
PUBLISH_RENDERERS = {"publish-v1": render_publish_v1, "publish-v2": render_publish_v2}


Renderer = TypeVar("Renderer", bound=Callable)


def renderer_for(renderers: Mapping[str, Renderer], version: str) -> Renderer:
    try:
        return renderers[version]
    except KeyError as exc:
        raise ValueError(f"unsupported template renderer: {version}") from exc
