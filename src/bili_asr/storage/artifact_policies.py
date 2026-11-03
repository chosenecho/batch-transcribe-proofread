"""Policy configuration and run journal persistence; callers own transactions."""
import json


class ArtifactPolicyRepository:
    def __init__(self, connection):
        self.connection = connection

    def configure(self, policy_id, configuration, *, now):
        self.connection.execute(
            "INSERT INTO artifact_policies VALUES (?,?,?,?,?) ON CONFLICT(policy_id) DO UPDATE SET "
            "mode=excluded.mode,target_id=excluded.target_id,config_json=excluded.config_json,updated_at=excluded.updated_at",
            (policy_id, configuration["mode"], configuration["target_id"], json.dumps(configuration, sort_keys=True), now))

    def begin_run(self, run_id, policy_id, policy_hash, configuration, *, retry_after, now):
        self.connection.execute("INSERT INTO artifact_policy_runs VALUES (?,?,?,?,NULL,NULL,'running',?,?,?)",
            (run_id, policy_id, policy_hash, json.dumps(configuration, sort_keys=True), retry_after, now, now))

    def freeze_plan(self, run_id, plan):
        self.connection.execute("UPDATE artifact_policy_runs SET plan_json=? WHERE run_id=?",
                                (json.dumps(plan, sort_keys=True), run_id))

    def finish_run(self, run_id, report, *, retry_after, now):
        self.connection.execute("UPDATE artifact_policy_runs SET state=?,result_json=?,retry_after=?,updated_at=? WHERE run_id=?",
            (report["state"], json.dumps(report, sort_keys=True), retry_after, now, run_id))
