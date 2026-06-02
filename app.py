import sqlite3
import json
import re
import math
import hashlib
from datetime import datetime
from typing import List, Optional, Dict, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

DB_PATH = "itte.db"

# =========================
# Database
# =========================

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_hash TEXT UNIQUE,
        repo TEXT,
        author TEXT,
        environment TEXT,
        change_type TEXT,
        title TEXT,
        diff TEXT,
        metadata_json TEXT,
        risk_score REAL,
        decision TEXT,
        reasons_json TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_id INTEGER,
        incident BOOLEAN,
        severity TEXT,
        notes TEXT,
        created_at TEXT,
        FOREIGN KEY(change_id) REFERENCES changes(id)
    )
    """)

    conn.commit()
    conn.close()

# =========================
# Models
# =========================

class ChangeRequest(BaseModel):
    repo: str = Field(..., example="acme/ai-agent")
    author: str = Field(..., example="alice")
    environment: str = Field(..., example="production")
    change_type: str = Field(
        ...,
        example="prompt",
        description="prompt | config | model | tool | code | policy"
    )
    title: str = Field(..., example="Update refund agent system prompt")
    diff: str = Field(..., example="- old prompt\n+ new prompt")
    metadata: Dict[str, Any] = Field(default_factory=dict)

class RiskResponse(BaseModel):
    change_id: int
    risk_score: float
    decision: str
    reasons: List[str]
    similar_incidents: List[Dict[str, Any]]

class OutcomeRequest(BaseModel):
    change_id: int
    incident: bool
    severity: str = Field(..., example="none | low | medium | high | critical")
    notes: str = ""

class OutcomeResponse(BaseModel):
    ok: bool
    message: str

# =========================
# Risk Engine
# =========================

HIGH_RISK_KEYWORDS = [
    "production",
    "prod",
    "payment",
    "billing",
    "refund",
    "delete",
    "drop",
    "truncate",
    "admin",
    "root",
    "secret",
    "token",
    "password",
    "credential",
    "pii",
    "gdpr",
    "hipaa",
    "compliance",
    "jailbreak",
    "ignore previous",
    "ignore all previous",
    "bypass",
    "disable safety",
    "no moderation",
    "autonomous",
    "auto approve",
    "execute",
    "shell",
    "database",
    "customer data",
    "personal data",
]

CRITICAL_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disable\s+(safety|moderation|guardrails?)",
    r"bypass\s+(policy|safety|approval)",
    r"auto[-\s]?approve",
    r"delete\s+.*customer",
    r"drop\s+table",
    r"truncate\s+table",
    r"export\s+.*(pii|personal data|customer data)",
]

def normalize_text(text: str) -> str:
    return text.lower().strip()

def tokenize(text: str) -> set:
    text = normalize_text(text)
    return set(re.findall(r"[a-zA-Z0-9_]+", text))

def jaccard_similarity(a: str, b: str) -> float:
    ta = tokenize(a)
    tb = tokenize(b)

    if not ta or not tb:
        return 0.0

    return len(ta & tb) / len(ta | tb)

def hash_change(req: ChangeRequest) -> str:
    payload = {
        "repo": req.repo,
        "environment": req.environment,
        "change_type": req.change_type,
        "title": req.title,
        "diff": req.diff,
        "metadata": req.metadata,
    }
    raw = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def load_historical_incidents(limit: int = 200):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        changes.id,
        changes.repo,
        changes.environment,
        changes.change_type,
        changes.title,
        changes.diff,
        changes.risk_score,
        outcomes.severity,
        outcomes.notes
    FROM changes
    JOIN outcomes ON changes.id = outcomes.change_id
    WHERE outcomes.incident = 1
    ORDER BY outcomes.created_at DESC
    LIMIT ?
    """, (limit,))

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows

def severity_weight(severity: str) -> float:
    mapping = {
        "none": 0.0,
        "low": 0.1,
        "medium": 0.2,
        "high": 0.3,
        "critical": 0.4,
    }
    return mapping.get(severity.lower(), 0.1)

def evaluate_risk(req: ChangeRequest):
    reasons = []
    score = 0.0

    full_text = f"""
    {req.repo}
    {req.environment}
    {req.change_type}
    {req.title}
    {req.diff}
    {json.dumps(req.metadata)}
    """.lower()

    # 1. Production risk
    if req.environment.lower() in ["prod", "production"]:
        score += 0.2
        reasons.append("Change targets production environment.")

    # 2. Change type risk
    type_risk = {
        "prompt": 0.15,
        "config": 0.15,
        "model": 0.25,
        "tool": 0.25,
        "code": 0.2,
        "policy": 0.3,
    }

    score += type_risk.get(req.change_type.lower(), 0.1)

    if req.change_type.lower() in ["model", "tool", "policy"]:
        reasons.append(f"Change type `{req.change_type}` is high-impact.")

    # 3. Keyword risk
    keyword_hits = []
    for kw in HIGH_RISK_KEYWORDS:
        if kw in full_text:
            keyword_hits.append(kw)

    if keyword_hits:
        added = min(0.25, 0.03 * len(keyword_hits))
        score += added
        reasons.append(
            "High-risk terms detected: " + ", ".join(sorted(set(keyword_hits))[:10])
        )

    # 4. Critical pattern risk
    pattern_hits = []
    for pattern in CRITICAL_PATTERNS:
        if re.search(pattern, full_text):
            pattern_hits.append(pattern)

    if pattern_hits:
        score += 0.35
        reasons.append("Critical unsafe pattern detected.")

    # 5. Large diff risk
    line_count = len(req.diff.splitlines())
    if line_count > 100:
        score += 0.15
        reasons.append(f"Large change detected: {line_count} lines.")

    elif line_count > 30:
        score += 0.08
        reasons.append(f"Medium-sized change detected: {line_count} lines.")

    # 6. Metadata signals
    if req.metadata.get("touches_customer_data") is True:
        score += 0.25
        reasons.append("Metadata says change touches customer data.")

    if req.metadata.get("requires_human_approval") is True:
        score += 0.15
        reasons.append("Metadata says change requires human approval.")

    if req.metadata.get("rollback_plan") in [None, "", False]:
        score += 0.08
        reasons.append("No rollback plan found.")

    # 7. Memory similarity
    incidents = load_historical_incidents()
    similar_incidents = []

    target_text = f"{req.change_type} {req.title} {req.diff}"

    for item in incidents:
        historical_text = f"{item['change_type']} {item['title']} {item['diff']}"
        sim = jaccard_similarity(target_text, historical_text)

        if sim >= 0.12:
            similar_incidents.append({
                "change_id": item["id"],
                "repo": item["repo"],
                "environment": item["environment"],
                "change_type": item["change_type"],
                "title": item["title"],
                "severity": item["severity"],
                "similarity": round(sim, 3),
                "notes": item["notes"],
            })

    similar_incidents = sorted(
        similar_incidents,
        key=lambda x: x["similarity"],
        reverse=True
    )[:5]

    if similar_incidents:
        strongest = similar_incidents[0]
        memory_boost = min(
            0.35,
            strongest["similarity"] + severity_weight(strongest["severity"])
        )
        score += memory_boost
        reasons.append(
            f"Similar historical incident found: change #{strongest['change_id']} "
            f"with severity `{strongest['severity']}`."
        )

    # Clamp
    score = max(0.0, min(1.0, score))

    if score >= 0.75:
        decision = "block"
        reasons.append("Risk score exceeds block threshold.")
    elif score >= 0.45:
        decision = "review"
        reasons.append("Risk score exceeds review threshold.")
    else:
        decision = "allow"
        reasons.append("Risk score below review threshold.")

    return {
        "risk_score": round(score, 3),
        "decision": decision,
        "reasons": reasons,
        "similar_incidents": similar_incidents,
    }

def save_change(req: ChangeRequest, risk_result: Dict[str, Any]):
    conn = get_conn()
    cur = conn.cursor()

    change_hash = hash_change(req)

    try:
        cur.execute("""
        INSERT INTO changes (
            change_hash,
            repo,
            author,
            environment,
            change_type,
            title,
            diff,
            metadata_json,
            risk_score,
            decision,
            reasons_json,
            created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            change_hash,
            req.repo,
            req.author,
            req.environment,
            req.change_type,
            req.title,
            req.diff,
            json.dumps(req.metadata),
            risk_result["risk_score"],
            risk_result["decision"],
            json.dumps(risk_result["reasons"]),
            datetime.utcnow().isoformat(),
        ))

        change_id = cur.lastrowid
        conn.commit()

    except sqlite3.IntegrityError:
        cur.execute("""
        SELECT id FROM changes WHERE change_hash = ?
        """, (change_hash,))
        row = cur.fetchone()
        change_id = row["id"]

    conn.close()
    return change_id

# =========================
# FastAPI App
# =========================

app = FastAPI(
    title="ITTE MVP",
    description="Self-evolving risk gate for AI engineering changes.",
    version="0.1.0",
)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "itte-mvp",
        "time": datetime.utcnow().isoformat(),
    }

@app.post("/risk/evaluate", response_model=RiskResponse)
def evaluate_change(req: ChangeRequest):
    risk_result = evaluate_risk(req)
    change_id = save_change(req, risk_result)

    return RiskResponse(
        change_id=change_id,
        risk_score=risk_result["risk_score"],
        decision=risk_result["decision"],
        reasons=risk_result["reasons"],
        similar_incidents=risk_result["similar_incidents"],
    )

@app.post("/outcomes", response_model=OutcomeResponse)
def record_outcome(req: OutcomeRequest):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT id FROM changes WHERE id = ?", (req.change_id,))
    row = cur.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Change not found.")

    cur.execute("""
    INSERT INTO outcomes (
        change_id,
        incident,
        severity,
        notes,
        created_at
    )
    VALUES (?, ?, ?, ?, ?)
    """, (
        req.change_id,
        1 if req.incident else 0,
        req.severity,
        req.notes,
        datetime.utcnow().isoformat(),
    ))

    conn.commit()
    conn.close()

    return OutcomeResponse(
        ok=True,
        message="Outcome recorded. ITTE memory updated."
    )

@app.get("/changes")
def list_changes(limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        id,
        repo,
        author,
        environment,
        change_type,
        title,
        risk_score,
        decision,
        created_at
    FROM changes
    ORDER BY created_at DESC
    LIMIT ?
    """, (limit,))

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()
    return rows

@app.get("/changes/{change_id}")
def get_change(change_id: int):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT * FROM changes WHERE id = ?
    """, (change_id,))

    row = cur.fetchone()

    if not row:
        conn.close()
        raise HTTPException(status_code=404, detail="Change not found.")

    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json"))
    result["reasons"] = json.loads(result.pop("reasons_json"))

    cur.execute("""
    SELECT * FROM outcomes WHERE change_id = ?
    ORDER BY created_at DESC
    """, (change_id,))

    result["outcomes"] = [dict(r) for r in cur.fetchall()]

    conn.close()
    return result

@app.get("/memory/incidents")
def list_incidents(limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT
        changes.id AS change_id,
        changes.repo,
        changes.environment,
        changes.change_type,
        changes.title,
        changes.risk_score,
        changes.decision,
        outcomes.severity,
        outcomes.notes,
        outcomes.created_at
    FROM outcomes
    JOIN changes ON changes.id = outcomes.change_id
    WHERE outcomes.incident = 1
    ORDER BY outcomes.created_at DESC
    LIMIT ?
    """, (limit,))

    rows = [dict(row) for row in cur.fetchall()]
    conn.close()

    return rows
