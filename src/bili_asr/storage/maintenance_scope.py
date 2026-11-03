"""One explicit selector model shared by export, import and backup plans."""
from __future__ import annotations

DATASETS = {
    "metadata": {"videos", "video_parts", "video_details", "source_videos", "source_creators", "bilibili_users",
                 "source_metadata_observations", "metadata_refresh_attempts"},
    "tags": {"video_tags", "video_tag_observations"},
    "workflow": {"workflow_jobs", "workflow_attempts", "workflow_job_dependencies", "workflow_asr_profiles",
                 "workflow_asr_profile_configs", "acquisition_runs", "acquisition_attempts"},
    "transcripts": {"transcripts", "transcript_segments", "transcript_asr_evidence", "audio_objects"},
    "editorial": {"editorial_inputs", "editorial_job_inputs", "editorial_input_versions", "editorial_model_calls",
                  "editorial_chunk_results", "editorial_revisions", "document_artifacts"},
    "publication": {"workflow_publications", "publication_requests", "publication_releases", "publication_events",
                    "publication_editions", "publication_edition_reviews", "publication_content_versions"},
}
ARTIFACTS = {"audio": "audio", "subtitles": "subtitles", "bundle": "transcripts",
             "documents": "documents", "publication": "publications"}


def scope(*, datasets=None, artifact_types=None, directories=None, since=None, until=None, statuses=None,
          profile="private-history", part_ids=None, bvids=None) -> dict:
    if profile not in {"private-history", "workflow-transcripts", "database-only", "database-subtitles", "publication"}:
        raise ValueError("unsupported recoverable maintenance profile")
    values = sorted(set(datasets or ()))
    if any(value not in DATASETS for value in values):
        raise ValueError("unknown maintenance dataset")
    artifacts = sorted(set(artifact_types or ()))
    if any(value not in ARTIFACTS for value in artifacts):
        raise ValueError("unknown maintenance artifact type")
    paths = sorted(set(directories or ()))
    from bili_asr.artifact_inventory import portable_artifact_parts
    for value in paths:
        portable_artifact_parts(value.rstrip("/") + "/selector")
    for value in (since, until):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError("scope timestamps must be nonnegative epoch seconds")
    if since is not None and until is not None and since > until:
        raise ValueError("scope time interval is reversed")
    states = sorted(set(statuses or ()))
    if any(not isinstance(value, str) or not value or len(value) > 64 for value in states):
        raise ValueError("invalid maintenance status selector")
    if profile == "workflow-transcripts" and not values:
        values = ["transcripts", "workflow"]
    parts, videos = sorted(set(part_ids or ())), sorted(set(bvids or ()))
    if any(type(value) is not int or value < 1 for value in parts) or any(not isinstance(value, str) or not value for value in videos):
        raise ValueError("invalid maintenance part or video selector")
    if profile == "publication" and any((values, artifacts, paths, parts, videos, states, since is not None, until is not None)):
        raise ValueError("publication backup selects current approved releases; private dataset selectors cannot change public permissions")
    return {"datasets": values, "artifact_types": artifacts, "directories": paths,
            "part_ids": parts, "bvids": videos,
            "since": since, "until": until, "statuses": states, "profile": profile}


def predicate(columns: list[str], specification: dict) -> tuple[str, tuple]:
    clauses, arguments = [], []
    timestamp = next((key for key in ("created_at", "started_at", "observed_at", "published_at", "discovered_at") if key in columns), None)
    if timestamp:
        if specification["since"] is not None:
            clauses.append(f'"{timestamp}">=?')
            arguments.append(specification["since"])
        if specification["until"] is not None:
            clauses.append(f'"{timestamp}"<=?')
            arguments.append(specification["until"])
    state = next((key for key in ("status", "outcome", "processing_status", "state") if key in columns), None)
    if specification["statuses"] and state:
        clauses.append(f'"{state}" IN ({",".join("?" for _ in specification["statuses"])})')
        arguments.extend(specification["statuses"])
    return " AND ".join(clauses) or "1", tuple(arguments)


def filter_artifacts(available: dict, required: set[str], specification: dict) -> tuple[dict, list[str]]:
    types = {ARTIFACTS[key] for key in specification["artifact_types"]}
    if specification["profile"] == "database-only":
        types = {"__none__"}
    elif specification["profile"] == "database-subtitles" and not types:
        types = {"subtitles", "transcripts"}
    directories = specification["directories"]
    included = {key: value for key, value in available.items() if
                (not types or key.split("/", 1)[0] in types)
                and (not directories or any(key.startswith(path.rstrip("/") + "/") for path in directories))}
    missing = required - set(included)
    if missing:
        raise ValueError("scope excludes referenced dependency; include its artifact type/directory: " + min(missing))
    return included, sorted(set(available) - set(included))
