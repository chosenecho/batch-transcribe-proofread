# Replay-index production acceptance (#512)

No production environment was available for this change. The following is a
runbook, not a deployment receipt. Source import, CLI readiness and a running
worker loading the accepted build are separate checks.

1. Build a complete committed release using `scripts/build_release.py`, retain
   its manifest and wheel, and follow `production-release.md`. Do not overwrite
   individual files in `site-packages` or use an editable checkout as a release.
2. Inventory every worker and external scheduler, stop new claims and drain
   active leases under the existing maintenance procedure. Preserve failures,
   retry holds, ledgers and unknown-send states. Retain a verified consistent
   full backup and the current environment before any index installation.
3. On a restored rehearsal copy, use the existing explicit `editorial
   replay-index --archive-root /rehearsal` maintenance command. Inspect its integrity
   report. Invalid JSON and request-identity ambiguity are evidence failures,
   not permission to replace historical request/response data.
4. From the candidate interpreter, run:

   ```sh
   python scripts/verify_replay_deployment.py --database /rehearsal/archive.db > replay-verification.json
   ```

   The read-only verifier checks the complete expression-index group, exact
   CASE/json_valid fingerprint expression, indexed EXPLAIN plans for the
   fingerprint and legacy scope, and bounded hit/miss replay with Python peak
   memory. It sends zero paid requests. The module checksum and PID belong only
   to the verifier. `worker_loaded_version_verified` deliberately remains false.
5. Repeat index installation and validation on the production database only
   after rehearsal and backup acceptance. Start the candidate through the
   release guard. Compare actual worker instance IDs, PIDs, startup and build
   health evidence using `scripts/production_health.py`. A changed source file
   or a verifier PID does not prove old workers were replaced.
6. Record real production-scale hit, miss and legacy replay, memory, bounded
   readback, and restoration of holds/attempts. Use retained evidence rather
   than invoking the paid model. Preserve the report and backup references.

For rollback, drain the candidate and retain a consistent current snapshot
before returning to the accepted environment. Readiness failure is not a reason
to purge calls, replay paid work, or rebuild the database without its artifacts.
Keep the original request/response evidence and the failed deployment record.
