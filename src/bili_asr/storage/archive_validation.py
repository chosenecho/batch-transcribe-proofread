"""Single read-only composition of installed optional archive contracts."""
from bili_asr.storage.extension_contracts import (
    require_artifact_catalog, require_artifact_online,
    require_import_extension, require_supplement_extension,
)
from bili_asr.storage.source_observations import require_observations

def require_optional_extensions(connection) -> None:
    require_artifact_catalog(connection)
    require_artifact_online(connection)
    require_import_extension(connection)
    require_supplement_extension(connection)
    require_observations(connection)
