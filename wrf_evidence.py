"""Immutable, user-scoped evidence drafts; review never grants execution authority."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
from typing import Literal
import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator
from wrf_knowledge import PHYSICS, TARGETS


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    locator: str = Field(min_length=1, max_length=200)
    excerpt: str = Field(min_length=1, max_length=3000)


class ParameterEvidence(Citation):
    option: int = Field(strict=True, ge=0, le=999)
    scheme_name: str = Field(min_length=1, max_length=200)


class EvidenceDraft(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    doi: str = Field(pattern=r"^10\.\d{4,9}/\S{1,200}$")
    source_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_version: str = Field(min_length=1, max_length=200)
    access_basis: Literal["open_access", "user_authorized_excerpt"]
    wrf_version: str = Field(min_length=1, max_length=80)
    region: str = Field(min_length=1, max_length=200)
    target: str
    season: str = Field(min_length=1, max_length=100)
    member_id: str = Field(min_length=1, max_length=100)
    domain_id: str = Field(min_length=1, max_length=100)
    dx_km: float = Field(gt=0, allow_inf_nan=False)
    context_evidence: Citation
    parameters: dict[str, ParameterEvidence] = Field(default_factory=dict, max_length=7)
    limitations: str = Field(min_length=1, max_length=2000)

    @field_validator("doi")
    @classmethod
    def normalize_doi(cls, value):
        return value.lower()

    @field_validator("target")
    @classmethod
    def known_target(cls, value):
        if value not in TARGETS:
            raise ValueError("不支持的目标变量")
        return value

    @field_validator("parameters")
    @classmethod
    def known_parameters(cls, value):
        if set(value) - set(PHYSICS.values()):
            raise ValueError("未知的物理参数字段")
        return value


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    decision: Literal["approve", "reject"]
    expected_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    note: str = Field(min_length=1, max_length=2000)
    source_checked: bool = Field(default=False, strict=True)
    mapping_checked: bool = Field(default=False, strict=True)


class EvidenceStore:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv("WRF_EVIDENCE_DB", str(Path(__file__).resolve().parents[1] / "knowledge/wrf/evidence.sqlite3")))

    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE IF NOT EXISTS drafts (
                id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL,
                digest TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS reviews (
                draft_id TEXT PRIMARY KEY REFERENCES drafts(id), reviewer TEXT NOT NULL,
                payload TEXT NOT NULL, created REAL NOT NULL);
        """)
        return db

    @staticmethod
    def _view(row, review):
        payload = json.loads(row["payload"])
        decision = json.loads(review["payload"]) if review else None
        return {"id": row["id"], "digest": row["digest"], "created_at": row["created"],
                "draft": payload, "missing_parameters": sorted(set(PHYSICS.values()) - set(payload["parameters"])),
                "status": ("reviewed_evidence" if decision["decision"] == "approve" else "rejected") if decision else "pending_review",
                "review": decision, "reviewed_at": review["created"] if review else None,
                "runnable": False, "runtime_validated": False,
                "warnings": ["原文哈希由提交者提供，服务未自行取得或核验全文", "人工审核不等于当前版本运行兼容性校验，也不代表最优方案"]}

    def create(self, draft: EvidenceDraft, owner: str):
        if not owner:
            raise PermissionError("缺少用户身份")
        payload = draft.model_dump_json()
        digest = hashlib.sha256(payload.encode()).hexdigest()
        draft_id = uuid.uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO drafts VALUES (?,?,?,?,?)", (draft_id, owner, payload, digest, time.time()))
        return self.get(draft_id, owner)

    def get(self, draft_id, owner, *, admin=False):
        if not owner:
            raise PermissionError("缺少用户身份")
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None or (row["owner"] != owner and not admin):
                raise LookupError("证据不存在或无权访问")
            review = db.execute("SELECT * FROM reviews WHERE draft_id=?", (draft_id,)).fetchone()
            return {**self._view(row, review), "can_review": bool(admin and row["owner"] != owner and review is None)}

    def list_drafts(self, owner, *, admin=False, offset=0, limit=20):
        if not owner:
            raise PermissionError("缺少用户身份")
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("无效分页参数")
        with closing(self.connect()) as db:
            where, args = ("", []) if admin else ("WHERE d.owner=?", [owner])
            rows = db.execute(f"""SELECT d.*, r.payload AS review_payload FROM drafts d
                LEFT JOIN reviews r ON r.draft_id=d.id {where}
                ORDER BY d.created DESC, d.id DESC LIMIT ? OFFSET ?""", [*args, limit + 1, offset]).fetchall()
            items = []
            for row in rows[:limit]:
                payload = json.loads(row["payload"])
                review = json.loads(row["review_payload"]) if row["review_payload"] else None
                items.append({"id": row["id"], "doi": payload["doi"], "region": payload["region"],
                              "domain_id": payload["domain_id"], "created_at": row["created"],
                              "status": ("reviewed_evidence" if review["decision"] == "approve" else "rejected") if review else "pending_review"})
            return {"items": items, "has_more": len(rows) > limit, "offset": offset}

    def review(self, draft_id, decision: ReviewDecision, reviewer, *, admin=False):
        if not reviewer or not admin:
            raise PermissionError("需要管理员审核权限")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if row is None:
                raise LookupError("证据不存在")
            if row["owner"] == reviewer:
                raise PermissionError("不能审核自己提交的证据")
            if row["digest"] != decision.expected_digest:
                raise ValueError("审核配置摘要不一致")
            existing = db.execute("SELECT * FROM reviews WHERE draft_id=?", (draft_id,)).fetchone()
            if existing:
                if existing["reviewer"] == reviewer and existing["payload"] == decision.model_dump_json():
                    return {**self._view(row, existing), "can_review": False}
                raise ValueError("该版本已有审核结论；请提交新草稿，不能覆盖历史")
            payload = json.loads(row["payload"])
            if decision.decision == "approve":
                if set(payload["parameters"]) != set(PHYSICS.values()):
                    raise ValueError("物理参数不完整，不能通过审核")
                if not decision.source_checked or not decision.mapping_checked:
                    raise ValueError("必须核对原文及方案编号映射")
            db.execute("INSERT INTO reviews VALUES (?,?,?,?)", (draft_id, reviewer, decision.model_dump_json(), time.time()))
        return self.get(draft_id, reviewer, admin=True)
