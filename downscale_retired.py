"""Read-only retirement boundary, independent of the former downscale package."""
from collections import Counter
from contextlib import closing
from pathlib import Path
import sqlite3
from typing import Callable

from fastapi import APIRouter, HTTPException, Request


TERMINAL = {"succeeded", "failed", "cancelled"}
ACTIVE = {"queued", "running", "preparing", "reconciling", "cancel_pending", "awaiting_review", "pending_review"}
SUMMARY_COLUMNS = ("id", "owner_sub", "status", "stage", "progress", "created_at", "updated_at", "archived_at")


class RetiredStateError(RuntimeError):
    pass


class RetiredDownscaleQuery:
    """Never create a DB, migrate a schema, start a runner or contact HPC."""

    def __init__(self, database_path):
        self.database_path = Path(database_path).resolve()

    def _rows(self, where="", parameters=(), *, limit=None):
        if not self.database_path.is_file():
            raise RetiredStateError("Retired task state is unavailable")
        try:
            with closing(sqlite3.connect(self.database_path.as_uri()+"?mode=ro",uri=True,timeout=2)) as db:
                db.row_factory = sqlite3.Row
                columns = {row["name"] for row in db.execute("PRAGMA table_info(downscale_tasks)")}
                if not {"id", "owner_sub", "status"}.issubset(columns):
                    raise RetiredStateError("Retired task schema is unavailable")
                selected = [name for name in SUMMARY_COLUMNS if name in columns]
                query = "SELECT "+",".join(selected)+" FROM downscale_tasks"
                if where:
                    query += " WHERE "+where
                query += " ORDER BY "+("created_at DESC,id" if "created_at" in columns else "id")
                if limit is not None:
                    query += " LIMIT ?"
                    parameters = (*parameters,max(1,min(200,int(limit))))
                return [dict(row) for row in db.execute(query,parameters)]
        except sqlite3.Error as exc:
            raise RetiredStateError("Retired task state cannot be read") from exc

    def lookup(self, task_id):
        rows = self._rows("id=?",(task_id,),limit=1)
        return rows[0] if rows else None

    def list_visible(self, owner, role, limit=50, archived=False):
        # There are no mutations; filtering preserves the legacy visibility boundary.
        rows = self._rows("owner_sub=?" if role < 3 else "",(owner,) if role < 3 else ())
        rows = [row for row in rows if bool(row.get("archived_at")) == archived]
        return [self.public_summary(row) for row in rows[:max(1,min(200,int(limit)))]]

    @staticmethod
    def public_summary(row):
        return {**{key:value for key,value in row.items() if key != "owner_sub"},
                "task_type":"high_resolution_downscale", "retired":True}

    def local_activity(self):
        try:
            rows = self._rows()
        except RetiredStateError:
            return {"retired":True,"database_readable":False,"task_count":None,
                    "active_task_count":0,"unknown_task_count":1,"downscale_active":True}
        counts = Counter(row["status"] for row in rows)
        active = sum(count for status,count in counts.items() if status in ACTIVE)
        unknown = sum(count for status,count in counts.items() if status not in TERMINAL | ACTIVE)
        return {"retired":True,"database_readable":True,"task_count":len(rows),
                "active_task_count":active,"unknown_task_count":unknown,"downscale_active":bool(active or unknown)}


def build_retired_router(query: RetiredDownscaleQuery, viewer: Callable[[Request],tuple[str,int]],
                         normal_tasks: Callable[[Request,int],list[dict]]):
    router = APIRouter(prefix="/api/wrf",tags=["retired-downscale"])

    def identity(request):
        owner,role = viewer(request)
        if not owner:
            raise HTTPException(401,"Token 缺少用户身份")
        return owner,role

    def visible(task_id,request):
        owner,role = identity(request)
        try:
            row = query.lookup(task_id)
        except RetiredStateError as exc:
            raise HTTPException(503,"退役任务状态暂不可读取") from exc
        if row is None or role < 3 and row["owner_sub"] != owner:
            raise HTTPException(404,"高分辨率任务不存在")
        return row

    def retired():
        raise HTTPException(410,detail={"code":"wrf_downscale_retired","message":"高分降尺度功能已退役，请使用普通 WRF 工作台。"})

    @router.get("/downscale/tasks")
    def tasks(request: Request,limit: int=50,archived: bool=False):
        owner,role = identity(request)
        try:
            items = query.list_visible(owner,role,limit,archived)
        except RetiredStateError as exc:
            raise HTTPException(503,"退役任务状态暂不可读取") from exc
        return {"code":0,"data":{"items":items,"retired":True},"message":"success"}

    @router.get("/downscale/tasks/{task_id}")
    def detail(task_id: str,request: Request):
        return {"code":0,"data":query.public_summary(visible(task_id,request)),"message":"success"}

    @router.get("/downscale/tasks/{task_id}/logs")
    @router.get("/downscale/tasks/{task_id}/result")
    def removed_asset(task_id: str,request: Request):
        visible(task_id,request)
        retired()

    @router.get("/workbench/tasks")
    def workbench(request: Request,limit: int=50):
        identity(request)
        result = normal_tasks(request,max(1,min(200,limit)))
        return {"code":0,"data":result if isinstance(result,dict) else {"items":result},"message":"success"}

    @router.api_route("/downscale/{path:path}",methods=["GET","POST","PUT","PATCH","DELETE"])
    def gone(path: str,request: Request):
        # Authentication precedes 410; payload parsing never invokes old schemas.
        identity(request)
        retired()

    return router
