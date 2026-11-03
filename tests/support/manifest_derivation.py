import time
import pytest
from bili_asr.services import manifest_derivation
from bili_asr.services.manifest_derivation import (
    QUEUE_STATUS,
    SKIP_ALREADY_DERIVED,
    SKIP_CHAIN_OWNED,
    SKIP_IDENTITY_MISMATCH,
    derive_rows,
    duration_s_from_ms,
    row_for_part,
)


PUBDATE = 1_700_000_000


def _part(bvid="BV1xx4y1zz", page_index=2, cid=987_654, duration_ms=1_800_000, **kw):
    """One ``dict(row)`` of the §2 relation, attempt evidence included."""
    row = {
        "video_part_id": 41,
        "work_id": f"{bvid}:p{page_index}",
        "bvid": bvid,
        "page_index": page_index,
        "cid": cid,
        "part_title": "第一部分：开场",
        "duration_ms": duration_ms,
        "attempted": 1,
        "last_attempt_at": 1_785_701_056,
        "last_attempt_outcome": "no-subtitle",
        "last_attempt_error_code": None,
        "last_attempt_credential_present": 0,
    }
    row.update(kw)
    return row
