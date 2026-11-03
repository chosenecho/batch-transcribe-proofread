"""Shared scalar classification of durable calls, using the fixed SQL alias m."""

# Only the old writer's complete synthetic response plus its committed chunk
# result proves a local operation without invocation_kind. A provider response
# with missing/invalid evidence always keeps its physical reservation consumed.
LEGACY_LOCAL_SQL = """CASE WHEN json_valid(m.response_json) THEN (
    m.finished_at IS NOT NULL AND m.error_code IS NULL
    AND json_type(m.response_json)='object'
    AND (SELECT count(*) FROM json_each(m.response_json))=4
    AND json_extract(m.response_json,'$.id')='local-precheck'
    AND json_extract(m.response_json,'$.model')='local-precheck'
    AND json_type(m.response_json,'$.choices')='array'
    AND json_array_length(m.response_json,'$.choices')=0
    AND json_type(m.response_json,'$.usage')='object'
    AND (SELECT count(*) FROM json_each(m.response_json,'$.usage'))=2
    AND json_type(m.response_json,'$.usage.prompt_tokens')='integer'
    AND json_extract(m.response_json,'$.usage.prompt_tokens')=0
    AND json_type(m.response_json,'$.usage.completion_tokens')='integer'
    AND json_extract(m.response_json,'$.usage.completion_tokens')=0
    AND EXISTS (SELECT 1 FROM editorial_chunk_results local_result
        WHERE local_result.input_id=m.input_id AND local_result.chunk_id=m.chunk_id
            AND local_result.call_id=m.call_id)
) ELSE 0 END"""

PHYSICAL_CALL_SQL = f"""CASE WHEN json_valid(m.request_json) THEN CASE
    WHEN json_extract(m.request_json,'$.invocation_kind') IN ('local_precheck','response_replay') THEN 0
    WHEN json_type(m.request_json,'$.invocation_kind') IS NULL AND ({LEGACY_LOCAL_SQL}) THEN 0
    ELSE 1 END ELSE 1 END"""
