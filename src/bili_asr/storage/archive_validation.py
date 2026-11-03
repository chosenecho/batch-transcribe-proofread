"""Single read-only composition of installed optional archive contracts."""
from bili_asr.storage.extension_contracts import (
    require_artifact_catalog, require_artifact_online,
    require_import_extension, require_supplement_extension,
)
from bili_asr.storage.source_observations import require_observations

def require_optional_extensions(connection) -> None:
    from bili_asr.storage.publication_package_contract import require_packages
    from bili_asr.storage.workflow_batch_contract import require_batches
    require_batches(connection)
    require_packages(connection)
    require_artifact_catalog(connection)
    require_artifact_online(connection)
    require_import_extension(connection)
    require_supplement_extension(connection)
    require_observations(connection)
