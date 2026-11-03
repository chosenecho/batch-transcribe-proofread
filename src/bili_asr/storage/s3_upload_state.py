"""Resumable part receipts inside the existing transfer ledger."""
import json
import time
import re

from bili_asr.canonical_json import canonical


class S3UploadState:
    def __init__(self, connection, transfer_id):
        self.connection, self.transfer_id = connection, transfer_id

    def load(self):
        row = self.connection.execute(
            "SELECT upload_id,part_size,parts_json FROM artifact_multipart_uploads WHERE transfer_id=?", (self.transfer_id,)).fetchone()
        if row is None:
            return None
        parts = json.loads(row[2])
        if (not isinstance(parts, list) or len(parts) > 10000
                or any(not isinstance(p, dict) or set(p) != {"PartNumber", "ETag", "sha256"}
                       or type(p["PartNumber"]) is not int or not 1 <= p["PartNumber"] <= 10000
                       or not isinstance(p["ETag"], str) or not 1 <= len(p["ETag"]) <= 1024
                       or not isinstance(p["sha256"], str) or not re.fullmatch("[0-9a-f]{64}", p["sha256"]) for p in parts)
                or len({p["PartNumber"] for p in parts}) != len(parts)):
            raise ValueError("invalid multipart resume receipts")
        return {"upload_id": row[0], "part_size": row[1], "parts": parts}

    def save(self, state):
        with self.connection:
            self.connection.execute(
                "INSERT INTO artifact_multipart_uploads VALUES (?,?,?,?,?) "
                "ON CONFLICT(transfer_id) DO UPDATE SET upload_id=excluded.upload_id,part_size=excluded.part_size,parts_json=excluded.parts_json,updated_at=excluded.updated_at",
                (self.transfer_id, state["upload_id"], state["part_size"], canonical(state["parts"]), int(time.time())))

    def clear(self):
        with self.connection:
            self.connection.execute("DELETE FROM artifact_multipart_uploads WHERE transfer_id=?", (self.transfer_id,))
