"""Prepare retained inputs before claiming business attempts."""
from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager

from bili_asr.archive_maintenance import ArchiveBusyError
from bili_asr.services.artifact_access import ArtifactAccess
from bili_asr.services.artifact_coordination import (
    consumer_pin,
    reserve_local_space,
    resource_fence,
)
from bili_asr.services.artifact_restore import restore_artifact
from bili_asr.services.workflow_audio_access import retained_audio, verified_local_audio, processing_audio
from bili_asr.storage.artifact_online import require_artifact_online
from bili_asr.workflow import WorkerInputUnavailable
from bili_asr.workflow_errors import JobExecutionError
from bili_asr.workflow_models import JobKind


def ensure_local(connection, roots, retained, *, storage_targets):
    """Deduplicate exact-object restore while the caller holds its read pin."""
    try:
        return verified_local_audio(connection, roots, retained)
    except JobExecutionError:
        pass
    with resource_fence(roots, "restore:" + retained["object_id"], exclusive=True):
        try:
            return verified_local_audio(connection, roots, retained)
        except JobExecutionError:
            pass
        from bili_asr.services.artifact_policy import minimum_free_space
        remaining = dict(storage_targets)
        failures = []
        while remaining:
            packaged = ArtifactAccess(connection, roots, remaining).locate_external(retained["object_id"])
            try:
                with reserve_local_space(connection, roots, retained["byte_size"], owner="restore:" + retained["object_id"],
                                         minimum_free_bytes=minimum_free_space(connection)):
                    restore_artifact(roots, retained["object_id"], target_id=packaged.target.target_id,
                                     target_root=packaged.target.root, storage_key=retained["storage_key"], _online=True)
                return verified_local_audio(connection, roots, retained)
            except (ValueError, OSError) as error:
                failures.append(packaged.target.target_id + ":" + type(error).__name__)
                remaining.pop(packaged.target.target_id)
        raise ValueError("artifact_input_unavailable: no supplied replica could restore exact audio; " + "; ".join(failures))


@contextmanager
def candidate_artifact_access(connection, repository, roots, job, *, storage_targets):
    """Keep the input fence until the executor finishes or abandons this claim."""
    if job.kind not in {JobKind.AUDIO, JobKind.ASR} or not require_artifact_online(connection):
        yield
        return
    previous = connection.execute("SELECT retry_after FROM artifact_input_states WHERE job_id=?", (job.job_id,)).fetchone()
    if previous is not None and previous[0] > time.time():
        raise WorkerInputUnavailable("artifact_input_backoff: previous input or capacity observation is still current")
    retained = None
    with ExitStack() as resources:
        try:
            result = processing_audio(connection, repository, job) if job.kind == JobKind.ASR else None
            retained = retained_audio(connection, job.video_part_id, result)
            if retained is not None:
                resources.enter_context(consumer_pin(connection, roots, retained["object_id"],
                    owner=f"prepare:{job.job_id}:{job.attempt_count}"))
                ensure_local(connection, roots, retained, storage_targets=storage_targets)
                # Decode/resample/alignment workspace is separate from the restored
                # compressed member. Reserve a bounded PCM working-set allowance.
                if job.kind == JobKind.ASR:
                    from bili_asr.services.artifact_policy import minimum_free_space
                    resources.enter_context(reserve_local_space(connection, roots,
                        max(16 * 1024**2, retained["duration_ms"] * 192), owner="asr:" + job.job_id,
                        minimum_free_bytes=minimum_free_space(connection)))
            elif job.kind == JobKind.AUDIO:
                from bili_asr.services.artifact_policy import download_reservation
                resources.enter_context(download_reservation(connection, roots, job.video_part_id, owner=job.job_id))
        except (ArchiveBusyError, ValueError, OSError, JobExecutionError) as exc:
            with connection:
                connection.execute("INSERT INTO artifact_input_states VALUES (?,?,'blocked',?,?,?) ON CONFLICT(job_id) DO UPDATE SET object_id=excluded.object_id,readiness=excluded.readiness,reason=excluded.reason,observed_at=excluded.observed_at,retry_after=excluded.retry_after",
                    (job.job_id, None if retained is None else retained["object_id"], type(exc).__name__, int(time.time()), int(time.time()) + 60))
            raise WorkerInputUnavailable(str(exc)) from exc
        with connection:
            connection.execute("INSERT INTO artifact_input_states VALUES (?,?,'ready','local_input_checked',?,0) ON CONFLICT(job_id) DO UPDATE SET object_id=excluded.object_id,readiness=excluded.readiness,reason=excluded.reason,observed_at=excluded.observed_at,retry_after=0",
                (job.job_id, None if retained is None else retained["object_id"], int(time.time())))
        yield
