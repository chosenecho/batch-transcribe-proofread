#!/usr/bin/env python3
"""Replay explicitly selected real provider calls without network or source writes."""
from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from bili_asr.services.editorial_evaluation import historical_recovery_report, load_sample, replay_sample
from bili_asr.services.editorial_provider_evaluation import evaluate_campaign, evaluate_provider


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--call-id", action="append", required=True)
    parser.add_argument("--allow-paid-calls", action="store_true",
                        help="Explicitly opt in to up to four provider calls, capped at 8192 output tokens each")
    parser.add_argument("--campaign", action="store_true",
                        help="One immutable ledger for up to five historical samples, at most twenty calls total")
    parser.add_argument("--historical-recovery", action="store_true",
                        help="Read-only natural follow-up comparison for up to fifty exact saved failures")
    parser.add_argument("--ledger", type=Path, help="Private durable SQLite experiment evidence (includes source prose)")
    parser.add_argument("--endpoint", default="https://api.deepseek.com")
    parser.add_argument("--model", default="deepseek-flash")
    args = parser.parse_args()
    maximum_samples = 50 if args.historical_recovery else 5
    if not 1 <= len(args.call_id) <= maximum_samples or len(set(args.call_id)) != len(args.call_id):
        parser.error(f"select one to {maximum_samples} distinct saved calls")
    if args.historical_recovery and (args.campaign or args.allow_paid_calls or args.ledger):
        parser.error("historical recovery comparison is read-only and cannot send provider calls")
    if args.ledger and not args.allow_paid_calls:
        parser.error("--ledger requires --allow-paid-calls")
    if args.campaign and not args.allow_paid_calls:
        parser.error("--campaign requires --allow-paid-calls")
    if args.allow_paid_calls and (args.ledger is None or (not args.campaign and len(args.call_id) != 1)):
        parser.error("provider evaluation requires one call or --campaign, and a private --ledger")
    if args.ledger and args.ledger.resolve() == args.database.resolve():
        parser.error("experiment ledger must differ from source database")
    try:
        if args.historical_recovery:
            report = historical_recovery_report(args.database, args.call_id)
        elif args.allow_paid_calls:
            samples = [load_sample(args.database, call_id) for call_id in args.call_id]
            if args.campaign:
                report = evaluate_campaign(samples, args.ledger, endpoint=args.endpoint, model=args.model,
                                           allow_paid_calls=True)
            else:
                report = evaluate_provider(samples[0], args.ledger, endpoint=args.endpoint, model=args.model,
                                           allow_paid_calls=True)
        else:
            samples = [load_sample(args.database, call_id) for call_id in args.call_id]
            report = {"schema": "editorial-evaluation-v1", "mode": "offline_replay",
                      "samples": [replay_sample(sample) for sample in samples],
                      "provider_parameter_enforcement": "not_verified"}
    except (ValueError, KeyError, TypeError, sqlite3.Error) as error:
        # Do not print exceptions containing SQL, paths or saved model content.
        parser.exit(1, f"Evidence replay failed ({type(error).__name__})\n")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
