"""Compatibility exports for explicit universal frozen content/input/template v2.

Implementation owners are separate: models, frozen input preparation, renderers.
The facade preserves existing imports without participating in their dependency graph.
"""
from bili_asr.publication_models_v2 import (
    INPUT_VERSION, CONTENT_VERSION, content_from_ai_v2, normalize_content_v2,
)
from bili_asr.editorial_input_v2 import MULTILINGUAL_PROMPT, prepare_input_v2
from bili_asr.manuscript_renderers_v2 import (
    AI_TEMPLATE_VERSION, PUBLISH_TEMPLATE_VERSION, render_ai_v2, render_publish_v2,
)

__all__ = ["INPUT_VERSION", "CONTENT_VERSION", "AI_TEMPLATE_VERSION", "PUBLISH_TEMPLATE_VERSION",
           "MULTILINGUAL_PROMPT", "prepare_input_v2", "normalize_content_v2", "content_from_ai_v2",
           "render_ai_v2", "render_publish_v2"]
