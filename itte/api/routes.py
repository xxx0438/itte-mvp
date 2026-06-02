import json
from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from itte.api.schemas import (
    ChangeRequest,
    RiskResponse,
    OutcomeRequest,
    JudgmentRequest,
    MemorySeedRequest,
    ApprovalDecisionRequest,
    OrgProfileRequest,
)
from itte.db import repository as repo
from itte.integrations.approval import maybe_create_approval
from itte.observability import (
    logger,
    EVALUATE_COUNTER,
    EVALUATE_LATENCY,
)
from itte.utils import safe_json_loads

router = APIRouter()

@router.get("/health")
def health():
    return {
        "ok": True,
        "service": "itte-mvp",
    }

@router.get("/metrics")
def metrics():
    return Response(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )

@router.post("/risk/evaluate", response_model=RiskResponse)
async def evaluate_change(req: ChangeRequest, request: Request):
    engine = request.app.state.risk_engine

    with EVALUATE_LATENCY.time():
        result = await engine.evaluate(req)

    change_id = repo.save_change(req, result)

    approval_id = maybe_create_approval(
        change_id=change_id,
        org=req.org,
        requested_by=req.author,
        decision=result["decision"],
    )

    EVALUATE_COUNTER.labels(decision=result["decision"]).inc()

    return RiskResponse(
        change_id=change_id,
        risk_score=result["risk_score"],
        decision=result["decision"],
        reasons=result["reasons"],
        compliance_findings=result["compliance_findings"],
        similar_memory=result["similar_memory"],
        approval_id=approval_id,
    )

@router.post("/outcomes")
async def record_outcome(req: OutcomeRequest, request: Request):
    change = repo.get_change(req.change_id)

    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    repo.record_outcome(
        change_id=req.change_id,
        incident=req.incident,
        severity=req.severity,
        notes=req.notes,
    )

    label = "allow"

    if req.incident and req.severity in ["high", "critical"]:
        label = "block"
    elif req.incident:
        label = "review"

    memory_text = f"""
    Outcome memory.
    change_type={change["change_type"]}
    title={change["title"]}
    diff={change["diff"]}
    incident={req.incident}
    severity={req.severity}
    notes={req.notes}
    """

    memory_id = repo.create_memory_item(
        source="private_incident",
        org=change["org"],
        text=memory_text,
        label=label,
        incident=req.incident,
        severity=req.severity,
        notes=req.notes,
        framework="OUTCOME",
    )

    await request.app.state.vector_store.add_one(memory_id, memory_text)

    return {
        "ok": True,
        "message": "Outcome recorded and memory updated.",
        "memory_id": memory_id,
    }

@router.post("/judgments")
async def record_judgment(req: JudgmentRequest, request: Request):
    change = repo.get_change(req.change_id)

    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    if req.label not in ["allow", "review", "block"]:
        raise HTTPException(status_code=400, detail="label must be allow/review/block")

    repo.record_judgment(
        change_id=req.change_id,
        reviewer=req.reviewer,
        label=req.label,
        confidence=req.confidence,
        rationale=req.rationale,
    )

    severity = "none"
    if req.label == "review":
        severity = "medium"
    elif req.label == "block":
        severity = "high"

    memory_text = f"""
    Senior engineer judgment.
    reviewer={req.reviewer}
    label={req.label}
    confidence={req.confidence}
    rationale={req.rationale}
    change_type={change["change_type"]}
    title={change["title"]}
    diff={change["diff"]}
    """

    memory_id = repo.create_memory_item(
        source="senior_judgment",
        org=change["org"],
        text=memory_text,
        label=req.label,
        incident=False,
        severity=severity,
        notes=req.rationale,
        framework="JUDGMENT_DISTILLATION",
    )

    await request.app.state.vector_store.add_one(memory_id, memory_text)

    return {
        "ok": True,
        "message": "Senior judgment distilled into memory.",
        "memory_id": memory_id,
    }

@router.post("/memory/seed")
async def seed_memory(req: MemorySeedRequest, request: Request):
    memory_id = repo.create_memory_item(
        source=req.source,
        org=req.org,
        text=req.text,
        label=req.label,
        incident=req.incident,
        severity=req.severity,
        notes=req.notes,
        framework=req.framework,
    )

    await request.app.state.vector_store.add_one(memory_id, req.text)

    return {
        "ok": True,
        "memory_id": memory_id,
    }

@router.get("/memory/search")
async def search_memory(q: str, request: Request, k: int = 5):
    hits = await request.app.state.vector_store.search(q, k=k)
    ids = [memory_id for memory_id, _ in hits]
    rows = repo.get_memory_items_by_ids(ids)

    by_id = {int(r["id"]): r for r in rows}

    result = []

    for memory_id, score in hits:
        item = by_id.get(memory_id)
        if not item:
            continue

        result.append({
            "memory_id": memory_id,
            "similarity": round(float(score), 3),
            "source": item["source"],
            "org": item["org"],
            "label": item["label"],
            "severity": item["severity"],
            "framework": item["framework"],
            "notes": item["notes"],
        })

    return {
        "items": result,
    }

@router.get("/changes")
def list_changes(limit: int = 50):
    return repo.list_changes(limit=limit)

@router.get("/changes/{change_id}")
def get_change(change_id: int):
    change = repo.get_change(change_id)

    if not change:
        raise HTTPException(status_code=404, detail="Change not found")

    change["metadata"] = safe_json_loads(change.pop("metadata_json"), {})
    change["reasons"] = safe_json_loads(change.pop("reasons_json"), [])
    change["compliance_findings"] = safe_json_loads(change.pop("compliance_json"), [])
    change["similar_memory"] = safe_json_loads(change.pop("similar_memory_json"), [])

    return change

@router.get("/approvals")
def list_approvals(status: str = None):
    return repo.list_approvals(status=status)

@router.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: int, req: ApprovalDecisionRequest):
    if req.status not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="status must be approved/rejected")

    repo.decide_approval(
        approval_id=approval_id,
        status=req.status,
        decided_by=req.decided_by,
        reason=req.reason,
    )

    return {"ok": True}

@router.get("/orgs/{org}/profile")
def get_org_profile(org: str):
    profile = repo.get_org_profile(org)
    profile["frameworks"] = json.loads(profile.get("frameworks_json", "[]"))
    return profile

@router.post("/orgs/{org}/profile")
def set_org_profile(org: str, req: OrgProfileRequest):
    repo.upsert_org_profile(
        org=org,
        risk_tolerance=req.risk_tolerance,
        regulated_industry=req.regulated_industry,
        frameworks=req.frameworks,
        review_threshold=req.review_threshold,
        block_threshold=req.block_threshold,
        memory_half_life_days=req.memory_half_life_days,
    )

    return {"ok": True}
