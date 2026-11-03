"""Run a configured archive backup policy under a local process supervisor."""
import argparse
import json
import signal
import threading
from contextlib import ExitStack
from pathlib import Path


def _run_policy(path, storage_targets, remote_bindings):
    from bili_asr.remote_storage import SSHDirectoryBackend, read_ssh_binding
    from bili_asr.services.artifact_access import parse_target_bindings
    from bili_asr.services.maintenance_backup import run_backup
    from bili_asr.services.maintenance_package import _json

    document = _json(path)
    if (storage_targets or remote_bindings) and document.get("media_policy") != "verified-references":
        raise ValueError("target bindings require explicit verified-references task-state policy")
    local_targets = parse_target_bindings(storage_targets)
    with ExitStack() as resources:
        backends = {}
        for binding_path in remote_bindings:
            binding = read_ssh_binding(binding_path)
            if binding.target_id in backends or binding.instance_id is None:
                raise ValueError("cold references require one exact remote instance per target")
            backends[binding.target_id] = resources.enter_context(SSHDirectoryBackend(binding))
        return run_backup(document, trigger="time", local_targets=local_targets, remote_backends=backends)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--storage-target", action="append", default=[], help="TARGET_ID=DIRECTORY for cold references")
    parser.add_argument("--remote-binding", action="append", type=Path, default=[], help="SSH target binding for cold references")
    args = parser.parse_args()
    if args.poll_seconds < 1 or not args.policy.is_absolute():
        parser.error("policy must be absolute and poll interval positive")
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    while not stop.is_set():
        try:
            result = _run_policy(args.policy, args.storage_target, args.remote_binding)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
        except (ValueError, OSError) as exc:
            print(json.dumps({"operation": "maintenance-backup", "valid": False,
                              "notification": "backup_configuration_failure", "error": str(exc)}), flush=True)
        stop.wait(args.poll_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
