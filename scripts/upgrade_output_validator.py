#!/usr/bin/env python3
"""Explicit operator upgrade of a completed attempt's output validator only."""
import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id")
    parser.add_argument("--validation-release")
    parser.add_argument("--output", type=Path, help="New private read-only plan/revalidation audit")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--plan", type=Path)
    parser.add_argument("--confirm-digest")
    parser.add_argument("--audit", type=Path)
    parser.add_argument("--maintenance-lock-fd", type=int, help="Reuse an inherited descriptor for the exact global maintenance lock")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    from config import settings
    from hpc_transport import HpcClient
    from output_validation_recovery import apply_plan, make_plan, write_private_json
    if args.timeout < 1 or args.timeout > 7200:
        parser.error("--timeout must be between 1 and 7200 seconds")
    if args.apply:
        if not all((args.plan, args.confirm_digest, args.audit)) or any((args.task_id, args.validation_release, args.output)):
            parser.error("--apply requires only --plan, --confirm-digest and a new --audit")
    elif not all((args.task_id, args.validation_release)) or any((args.plan, args.confirm_digest, args.audit, args.maintenance_lock_fd is not None)):
        parser.error("Read-only inspection requires --task-id and --validation-release")
    if args.maintenance_lock_fd is not None and args.maintenance_lock_fd < 0:
        parser.error("--maintenance-lock-fd must be a nonnegative inherited descriptor")
    client = HpcClient(settings)
    try:
        if args.apply:
            with ExitStack() as stack:
                inherited = (stack.enter_context(os.fdopen(os.dup(args.maintenance_lock_fd), "r"))
                             if args.maintenance_lock_fd is not None else None)
                result = apply_plan(json.loads(args.plan.read_text()), args.confirm_digest, settings, client,
                                    args.audit, held_lock=inherited, timeout=args.timeout)
        else:
            result = make_plan(settings, client, args.task_id, args.validation_release, timeout=args.timeout)
            if args.output:
                write_private_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("blockers") else 0
    finally:
        client.close_session()


if __name__ == "__main__":
    raise SystemExit(main())
