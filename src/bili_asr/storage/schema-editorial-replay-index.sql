-- editorial-replay-index/v1 is derived, nonunique and explicitly installed.
CREATE INDEX editorial_replay_fingerprint_v1 ON editorial_model_calls (
    CASE WHEN json_valid(request_json) THEN json_extract(request_json, '$.request_fingerprint') END,
    finished_at DESC
) WHERE response_json IS NOT NULL AND error_code IS NULL;
CREATE INDEX editorial_replay_scope_v1 ON editorial_model_calls (
    input_id, chunk_id, finished_at DESC
) WHERE response_json IS NOT NULL AND error_code IS NULL;
