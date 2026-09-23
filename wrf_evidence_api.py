"""Manual evidence review endpoints. Not registered as LLM tools."""
from fastapi import APIRouter, HTTPException, Request, Query
from wrf_evidence import EvidenceDraft, EvidenceStore, ReviewDecision

router = APIRouter(prefix="/api/wrf/evidence")


def identity(request):
    payload = getattr(request.state, "user", {}) or {}
    owner = str(payload.get("sub") or "")
    try:
        role = int(payload.get("role", 0))
    except (TypeError, ValueError):
        role = 0
    if not owner:
        raise HTTPException(401, "需要有效用户身份")
    if role < 2:
        raise HTTPException(403, "权限不足")
    return owner, role >= 3


def respond(operation):
    try:
        return {"code": 0, "data": operation()}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.post("", status_code=201)
def create_evidence(payload: EvidenceDraft, request: Request):
    owner, _ = identity(request)
    return respond(lambda: EvidenceStore().create(payload, owner))


@router.get("")
def list_evidence(request: Request, offset: int = Query(default=0, ge=0), limit: int = Query(default=20, ge=1, le=100)):
    owner, admin = identity(request)
    return respond(lambda: EvidenceStore().list_drafts(owner, admin=admin, offset=offset, limit=limit))


@router.get("/{draft_id}")
def get_evidence(draft_id: str, request: Request):
    owner, admin = identity(request)
    return respond(lambda: EvidenceStore().get(draft_id, owner, admin=admin))


@router.post("/{draft_id}/review")
def review_evidence(draft_id: str, payload: ReviewDecision, request: Request):
    owner, admin = identity(request)
    return respond(lambda: EvidenceStore().review(draft_id, payload, owner, admin=admin))
