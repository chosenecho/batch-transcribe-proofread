"""Application caption-selection policy, separate from transport adapters."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CaptionPolicy:
    version: str
    selection: str
    extractor: str
    translation_only_error: str
    candidate_budget_error: str
    body_unavailable_error: str
    access_unverified_error: str
    candidate_limit: int = 32

    def __post_init__(self):
        if type(self.candidate_limit) is not int or self.candidate_limit < 1:
            raise ValueError("caption candidate budget must be positive")


YOUTUBE_CAPTION_POLICY = CaptionPolicy(
    "youtube-public-v1", "youtube-original-captions-v1", "yt-dlp",
    "youtube_caption_translation_only", "youtube_caption_candidate_budget",
    "youtube_caption_body_unavailable", "youtube_access_unverified")
