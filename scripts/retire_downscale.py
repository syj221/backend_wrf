#!/usr/bin/env python3
"""Inventory downscale retirement; application requires an approved manifest digest."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id",action="append",help="Exact downscale task ID; repeat for multiple tasks")
    parser.add_argument("--output",type=Path,help="Private inventory JSON path; never overwrites an existing file")
    parser.add_argument("--local-only",action="store_true",help="Incomplete read-only local inventory; cannot be applied")
    parser.add_argument("--environment-ownership",type=Path,help="Audited dedicated environment ownership JSON; adds its exact roots to the inventory")
    parser.add_argument("--reuse-remote-inventory",type=Path,help="Reuse prior file SHA evidence after a fresh complete metadata check; inventory only, apply always hashes every file")
    parser.add_argument("--remote-timeout",type=int,default=3600)
    parser.add_argument("--apply",action="store_true",help="Delete only the assets and records in the approved manifest")
    parser.add_argument("--manifest",type=Path)
    parser.add_argument("--confirm-digest",help="The single explicit confirmation, equal to the approved manifest digest")
    parser.add_argument("--audit",type=Path,help="Required separate minimal private audit for apply")
    parser.add_argument("--legacy-manifest",type=Path,help="Explicit intact legacy inventory supplying retired environment paths; inventory only")
    parser.add_argument("--maintenance-lock-fd",type=int,help="Inherited exact global maintenance lock; apply only")
    args = parser.parse_args()
    from config import settings
    from hpc_transport import HpcClient
    from DB.config import database_url
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url
    from retirement import make_manifest,apply_manifest,bind_legacy_settings
    if args.output and args.output.exists():
        parser.error("--output already exists; retain the previous inventory")
    url = make_url(database_url())
    if url.get_backend_name() == "sqlite":
        database = Path(url.database).expanduser().resolve()
        if not database.is_file():
            parser.error("Catalog database does not exist; inventory never initializes a database")
        if not args.apply:
            url = url.set(database="file:"+str(database),query={**url.query,"mode":"ro","uri":"true"})
    engine = create_engine(url)
    try:
        client = HpcClient(settings)
        if args.apply:
            if not args.manifest or not args.confirm_digest or not args.audit or args.local_only or args.task_id or args.environment_ownership or args.reuse_remote_inventory or args.legacy_manifest:
                parser.error("--apply requires --manifest, --confirm-digest and --audit, without --task-id/--local-only")
            from contextlib import nullcontext
            import os
            lock = (os.fdopen(os.dup(args.maintenance_lock_fd),"a")
                    if args.maintenance_lock_fd is not None else nullcontext(None))
            with lock as held_lock:
                result = apply_manifest(json.loads(args.manifest.read_text()),args.confirm_digest,settings,client,engine,args.audit,
                                        timeout=args.remote_timeout,held_lock=held_lock)
        else:
            if not args.task_id or args.manifest or args.confirm_digest or args.audit or args.maintenance_lock_fd is not None:
                parser.error("Inventory requires explicit --task-id values")
            ownership = json.loads(args.environment_ownership.read_text()) if args.environment_ownership else None
            cache = json.loads(args.reuse_remote_inventory.read_text()) if args.reuse_remote_inventory else None
            if cache and args.local_only:
                parser.error("--reuse-remote-inventory requires a fresh remote metadata check")
            inventory_settings = bind_legacy_settings(settings,json.loads(args.legacy_manifest.read_text())) if args.legacy_manifest else settings
            if ownership and not args.legacy_manifest:
                parser.error("Dedicated environment inventory requires explicit --legacy-manifest")
            result = make_manifest(inventory_settings,client,engine,args.task_id,remote=not args.local_only,timeout=args.remote_timeout,environment_ownership=ownership,remote_inventory_cache=cache)
        if args.output:
            import os
            args.output.parent.mkdir(parents=True,exist_ok=True)
            descriptor = os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,"w") as stream:
                json.dump(result,stream,ensure_ascii=False,indent=2)
            print(json.dumps({"output":str(args.output),"digest":result.get("digest"),"summary":result.get("summary"),"blockers":result.get("blockers",[])},ensure_ascii=False))
        else:
            print(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
