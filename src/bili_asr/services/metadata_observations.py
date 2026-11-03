"""Bind metadata providers to explicit private observation storage."""
from bili_asr.storage.source_observations import ObservationRepository, require_observations
from bili_asr.storage.metadata import MetadataRepository
from bili_asr.source_observations import PlatformObservation


def bind_observations(gateway: object, repository: MetadataRepository) -> bool:
    setter = getattr(gateway, "set_observation_sink", None)
    installed = require_observations(repository.connection)
    if setter is None:
        return False
    if not installed:
        setter(None)
        return False

    def append(observation: PlatformObservation) -> None:
        # Commit each real response before normalized page writes. A later
        # malformed item or failed endpoint cannot erase already obtained raw
        # evidence. Upstream calls run on this SQLite connection's owner thread.
        with repository.transaction():
            ObservationRepository(repository.connection).append((observation,))

    setter(append)
    return True
