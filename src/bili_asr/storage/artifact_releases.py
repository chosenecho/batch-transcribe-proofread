"""Release journal transitions on the caller's existing catalog connection."""
import json
import time


class ArtifactReleaseRepository:
    def __init__(self, connection):
        self.connection = connection

    def plan(self, *, transfer_id, copy_id, object_id, source_target_id, source_key,
             quarantine_key, source_replica_id, source_generation, now=None):
        transfer = self.connection.execute("SELECT kind FROM artifact_transfers WHERE transfer_id=?", (transfer_id,)).fetchone()
        if transfer is None or transfer[0] != "offload":
            raise ValueError("release intents require an offload transfer")
        now = int(time.time()) if now is None else now
        self.connection.execute("INSERT INTO artifact_release_intents VALUES (?,?,?,?,?,?,?,?, 'planned',?,?)",
            (transfer_id, copy_id, object_id, source_target_id, source_key, quarantine_key,
             source_replica_id, json.dumps(source_generation, sort_keys=True), now, now))

    def isolate(self, transfer_id, copy_id):
        self.connection.execute("UPDATE artifact_release_intents SET state='isolated',updated_at=? "
                                "WHERE transfer_id=? AND copy_id=?", (int(time.time()), transfer_id, copy_id))

    def record_outcome(self, transfer_id, copy_id, state):
        if state not in {"released", "cancelled", "isolated"}:
            raise ValueError("unsupported release outcome")
        self.connection.execute("UPDATE artifact_release_intents SET state=?,updated_at=? "
                                "WHERE transfer_id=? AND copy_id=?", (state, int(time.time()), transfer_id, copy_id))

    def complete_released_items(self, transfer_id):
        for identity, in self.connection.execute("SELECT object_id FROM artifact_transfer_items WHERE transfer_id=?", (transfer_id,)):
            pending = self.connection.execute("SELECT 1 FROM artifact_release_intents "
                "WHERE transfer_id=? AND object_id=? AND state!='released'", (transfer_id, identity)).fetchone()
            if pending is None:
                self.connection.execute("UPDATE artifact_transfer_items SET state='released',updated_at=? "
                    "WHERE transfer_id=? AND object_id=?", (int(time.time()), transfer_id, identity))
