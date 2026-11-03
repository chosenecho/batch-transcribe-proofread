"""Compatibility exports loaded from their owners only when requested."""
from importlib import import_module

_EXPORTS = {
    'DatabaseConnection': ('bili_asr.storage.database', 'DatabaseConnection'),
    'SchemaContractError': ('bili_asr.storage.database', 'SchemaContractError'),
    'duration_to_ms': ('bili_asr.storage.database', 'duration_to_ms'),
    'initialize_schema': ('bili_asr.storage.database', 'initialize_schema'),
    'normalize_page_index': ('bili_asr.storage.database', 'normalize_page_index'),
    'open_database': ('bili_asr.storage.database', 'open_database'),
    'refresh_shipped_views': ('bili_asr.storage.database', 'refresh_shipped_views'),
    'require_subtitle_schema': ('bili_asr.storage.database', 'require_subtitle_schema'),
    'MetadataRepository': ('bili_asr.storage.metadata', 'MetadataRepository'),
    'TranscriptRepository': ('bili_asr.storage.transcripts', 'TranscriptRepository'),
    'ALLOWED_ACQUISITION_KINDS': ('bili_asr.storage.models', 'ALLOWED_ACQUISITION_KINDS'),
    'ALLOWED_ACQUISITION_OUTCOMES': ('bili_asr.storage.models', 'ALLOWED_ACQUISITION_OUTCOMES'),
    'ALLOWED_ATTEMPT_OUTCOMES': ('bili_asr.storage.models', 'ALLOWED_ATTEMPT_OUTCOMES'),
    'ALLOWED_CAPTION_SOURCE_KINDS': ('bili_asr.storage.models', 'ALLOWED_CAPTION_SOURCE_KINDS'),
    'ALLOWED_CURSOR_STATES': ('bili_asr.storage.models', 'ALLOWED_CURSOR_STATES'),
    'ALLOWED_LOCAL_TRANSCRIPT_SOURCE_KINDS': ('bili_asr.storage.models', 'ALLOWED_LOCAL_TRANSCRIPT_SOURCE_KINDS'),
    'ALLOWED_PAGE_OUTCOMES': ('bili_asr.storage.models', 'ALLOWED_PAGE_OUTCOMES'),
    'ALLOWED_PROCESSING_STATUS': ('bili_asr.storage.models', 'ALLOWED_PROCESSING_STATUS'),
    'ALLOWED_RUN_OUTCOMES': ('bili_asr.storage.models', 'ALLOWED_RUN_OUTCOMES'),
    'ALLOWED_SOURCE_KINDS': ('bili_asr.storage.models', 'ALLOWED_SOURCE_KINDS'),
    'MAX_TIMELINE_MS': ('bili_asr.storage.models', 'MAX_TIMELINE_MS'),
    'AcquisitionKind': ('bili_asr.storage.models', 'AcquisitionKind'),
    'AcquisitionOutcome': ('bili_asr.storage.models', 'AcquisitionOutcome'),
    'AcquisitionRunRecord': ('bili_asr.storage.models', 'AcquisitionRunRecord'),
    'AttemptOutcome': ('bili_asr.storage.models', 'AttemptOutcome'),
    'CursorRecord': ('bili_asr.storage.models', 'CursorRecord'),
    'CursorState': ('bili_asr.storage.models', 'CursorState'),
    'DiscoveryRecord': ('bili_asr.storage.models', 'DiscoveryRecord'),
    'IngestionPageRecord': ('bili_asr.storage.models', 'IngestionPageRecord'),
    'IngestionRunRecord': ('bili_asr.storage.models', 'IngestionRunRecord'),
    'PageOutcome': ('bili_asr.storage.models', 'PageOutcome'),
    'ProcessingStatus': ('bili_asr.storage.models', 'ProcessingStatus'),
    'RunOutcome': ('bili_asr.storage.models', 'RunOutcome'),
    'SourceKind': ('bili_asr.storage.models', 'SourceKind'),
    'TranscriptRecord': ('bili_asr.storage.models', 'TranscriptRecord'),
    'TranscriptSegmentRecord': ('bili_asr.storage.models', 'TranscriptSegmentRecord'),
    'TranscriptWriteResult': ('bili_asr.storage.models', 'TranscriptWriteResult'),
    'UserRecord': ('bili_asr.storage.models', 'UserRecord'),
    'VideoDetailRecord': ('bili_asr.storage.models', 'VideoDetailRecord'),
    'VideoPartRecord': ('bili_asr.storage.models', 'VideoPartRecord'),
    'VideoRecord': ('bili_asr.storage.models', 'VideoRecord'),
    'VideoTagRecord': ('bili_asr.storage.models', 'VideoTagRecord'),
    'validate_error_code': ('bili_asr.storage.models', 'validate_error_code'),
    'AsrPolicy': ('bili_asr.storage.workflow', 'AsrPolicy'),
    'AsrProfile': ('bili_asr.storage.workflow', 'AsrProfile'),
    'JobKind': ('bili_asr.storage.workflow', 'JobKind'),
    'JobStatus': ('bili_asr.storage.workflow', 'JobStatus'),
    'WorkflowJob': ('bili_asr.storage.workflow', 'WorkflowJob'),
    'WorkflowPlan': ('bili_asr.storage.workflow', 'WorkflowPlan'),
    'WorkflowRepository': ('bili_asr.storage.workflow', 'WorkflowRepository'),
}


def __getattr__(name):
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module), attribute)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(_EXPORTS))


__all__ = list(_EXPORTS)
