"""Fixed hashes captured from 98904ed for original synthetic v1/v2 content."""
from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from bili_asr.canonical_json import digest
from bili_asr.editorial import EditorialConfig, prepare_input
from bili_asr.editorial_input_v2 import prepare_input_v2
from bili_asr.manuscript_templates import AI_RENDERERS, PUBLISH_RENDERERS, renderer_for
from bili_asr.platform_identity import ContentRef
from bili_asr.publication_content import content_from_ai
from bili_asr.publication_models_v2 import content_from_ai_v2
from bili_asr.source_metadata import SourceMetadataSnapshot


FROZEN_CONFIG = {
    "model": "deepseek-flash", "base_url": "https://api.deepseek.com",
    "context_tokens": 1_048_576, "max_input_tokens": 480_000,
    "max_output_tokens": 262_144, "safety_tokens": 16_384,
    "context_segments": 1, "timeout_seconds": 1800, "thinking_mode": "enabled",
    "reasoning_effort": "high", "top_p": 0.95, "temperature": None,
    "request_policy": "deepseek-v2", "max_chunk_regenerations": 2,
    "rule_version": "readable-prose-v2", "max_chunk_segments": 1,
    "max_chunk_chars": 20_000,
}


class FrozenFixtureConfig(EditorialConfig):
    """The exact 98904ed serialized settings, using the historical planner.

    Today's writer includes a new token policy and preset. This fixture keeps
    the original settings and layout so its captured hashes remain meaningful.
    """

    def to_dict(self):
        values = super().to_dict()
        return {key: values[key] for key in FROZEN_CONFIG}


def frozen_case(version, prepare, content_builder, render_ai, render_publish):
    """Exercise input/reference IDs, escaping, metadata and paired output bytes."""
    body = [SimpleNamespace(start_ms=0, end_ms=900, text="Original <speech> 中文。"),
            SimpleNamespace(start_ms=1000, end_ms=1900, text="Second & final sentence.")]
    base = SimpleNamespace(transcript_id=7, video_part_id=3, source_kind="asr-local",
                           language="English", content_sha256=digest([s.text for s in body]), segments=body)
    captions = [SimpleNamespace(start_ms=0, end_ms=800, text="Reference caption.")]
    reference = SimpleNamespace(transcript_id=8, video_part_id=3, source_kind="subtitle-cc",
                                language="en", content_sha256=digest([s.text for s in captions]), segments=captions)
    config = FrozenFixtureConfig.from_snapshot(FROZEN_CONFIG)
    assert config.budget_policy == "legacy-v1"
    assert config.to_dict() == FROZEN_CONFIG
    if version == 1:
        prepared = prepare(base, reference, config, metadata={
            "title": "Original <title> 中文", "bvid": "BV1xx411c7mD", "page_index": 0})
    else:
        metadata = SourceMetadataSnapshot(ContentRef("youtube", "dQw4w9WgXcQ", 0),
            "Original <title> 中文", "UCoriginal", "Original author", 1700000000, 1710000000)
        prepared = prepare(base, reference, config, source_metadata=metadata)
    blocks = [{"segment_ids": [f"t7:s{i}"], "start_ms": s.start_ms, "end_ms": s.end_ms,
               "original_text": s.text, "text": s.text, "issues": []} for i, s in enumerate(body)]
    revision = digest({"input": prepared["input_id"], "blocks": blocks})
    documents = render_ai(prepared["snapshot"]["metadata"], prepared, blocks, revision)
    content = content_builder(prepared, documents["ai-draft.md"])
    artifacts = {name: hashlib.sha256(text.encode("utf-8")).hexdigest() for name, text in documents.items()}
    artifacts["publish.md"] = hashlib.sha256(render_publish(content)).hexdigest()
    return {"inputId": prepared["input_id"], "preparedSha256": digest(prepared),
            "contentSha256": digest(content), "artifacts": artifacts}


# Captured using the unmodified functions at 98904ed, before moving owners.
EXPECTED = {
    1: {
        "inputId": "928fab2d732ff3c9cca73b4abedc015298920e5213dbb354a52bc685d342b517",
        "preparedSha256": "6719ab7fd571b2e3b909d32cc9ba92cd9598ded775385241ff2db40bffb6a2c6",
        "contentSha256": "2ada87c56a90240163ac2a740b9d65ea43175f61bcd08990f870ff2386bb606b",
        "artifacts": {
            "ai-draft.md": "d165b3a8dcfa2e885a46146bd7fbaa4155a6ceea55a77f710eac67c3c6ddfdc6",
            "review.md": "7ad706e6da027b835d522b6554e04969fd888b6aaad30a12e9649a5477d65ff9",
            "publish.md": "7686b7040861f42ed885fd5b94067b15fd0a1e56f23d8cdde30282910264914a",
        },
    },
    2: {
        "inputId": "ecebc9504d165f37533b4c57d168a3ab78d91d9c5fe731d29403a97f7b4dcf83",
        "preparedSha256": "9be3e260e1938c6054fefb2cd42b3682c89e5abd70991f4cadfbda5a069eae12",
        "contentSha256": "32b2e152c9854ba96cda35cba6adcce8673fa18983fbfbcc4746553b34ade9f8",
        "artifacts": {
            "ai-draft.md": "d165b3a8dcfa2e885a46146bd7fbaa4155a6ceea55a77f710eac67c3c6ddfdc6",
            "review.md": "5926ab9c0a51f91452db2e8e0bd26a41d622da16d9704e283c64a1e5e030d1a2",
            "publish.md": "57f855407caaa0786bb4f279c1a841ae7a831259ca5740ef2361cfb794e6a0b6",
        },
    },
}


@pytest.mark.parametrize("version", [1, 2])
def test_frozen_inputs_content_and_renderer_bytes_keep_original_hashes(version):
    actual = frozen_case(version, prepare_input if version == 1 else prepare_input_v2,
        content_from_ai if version == 1 else content_from_ai_v2,
        renderer_for(AI_RENDERERS, f"ai-draft-v{version}"),
        renderer_for(PUBLISH_RENDERERS, f"publish-v{version}"))
    assert actual == EXPECTED[version]


def test_unknown_template_does_not_fall_back_to_current_writer():
    with pytest.raises(ValueError, match="unsupported template renderer: publish-v99"):
        renderer_for(PUBLISH_RENDERERS, "publish-v99")
