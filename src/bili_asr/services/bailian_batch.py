from __future__ import annotations

import sqlite3
import subprocess
import time
from contextlib import closing
from pathlib import Path

from bili_asr.asr.bailian import MODEL, CloudError, transcribe, write_json
from bili_asr.services.transcription_targets import load_youtube_targets


def run_youtube_batch(
    *,
    archive_root: Path,
    input_path: Path,
    output_root: Path,
    api_key: str,
    retry_failed: bool = False,
) -> int:
    key = api_key
    try:
        targets = load_youtube_targets(input_path=input_path)
    except TypeError:
        raise ValueError("invalid_youtube_target_list") from None
    output_root.mkdir(parents=True, exist_ok=True)
    report = {"model": MODEL, "selected": len(targets), "results": []}
    with closing(
        sqlite3.connect(
            (archive_root / "archive.db").resolve().as_uri() + "?mode=ro", uri=True
        )
    ) as connection:
        connection.row_factory = sqlite3.Row
        for target in targets:
            identity = target.ref.external_video_id
            row = connection.execute(
                "SELECT a.storage_key,a.sha256,v.title FROM source_videos v "
                "JOIN video_parts p ON p.source_video_id=v.source_video_id JOIN part_audio_objects l "
                "ON l.video_part_id=p.video_part_id JOIN audio_objects a ON a.audio_id=l.audio_id "
                "WHERE v.platform='youtube' AND v.external_id=? AND p.page_index=0",
                (identity,),
            ).fetchone()
            item = {**target.as_dict(), "state": "audio_pending"}
            if row:
                path = (archive_root / row["storage_key"]).resolve()
                if not path.is_relative_to(archive_root.resolve()):
                    raise ValueError("audio_path_outside_archive")
                item.update(title=row["title"])
                try:
                    print(f"transcribing {identity}", flush=True)
                    item.update(
                        transcribe(
                            path,
                            output_root / identity,
                            api_key=key,
                            expected_sha256=row["sha256"],
                            retry_failed=retry_failed,
                        )
                    )
                    item["state"] = (
                        "needs_review" if item["requires_review"] else "succeeded"
                    )
                except (
                    CloudError,
                    ValueError,
                    OSError,
                    subprocess.SubprocessError,
                ) as exc:
                    # Provider codes and local fixed messages only; no raw HTTP objects.
                    code = (
                        str(exc)
                        if isinstance(exc, (CloudError, ValueError))
                        else type(exc).__name__
                    ).replace(key, "[REDACTED]")
                    item.update(state="failed", error_code=code)
            report["results"].append(item)
            report["updated_at"] = int(time.time())
            write_json(output_root / "batch-status.json", report)
            print(f"{identity}: {item['state']}", flush=True)
            if item.get("error_code") in {
                "InvalidApiKey",
                "AccessDenied",
                "Arrearage",
                "QuotaExhausted",
            }:
                break
    return (
        0
        if len(report["results"]) == len(targets)
        and all(x["state"] == "succeeded" for x in report["results"])
        else 1
    )
