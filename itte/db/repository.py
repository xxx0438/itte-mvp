import json
import sqlite3
from typing import Any, Dict, List, Optional

from itte.config import settings
from itte.utils import utc_now, stable_hash
from itte.observability import logger
from itte.api.schemas import ChangeRequest

def get_conn():
    conn = sqlite3.connect(settings.db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    logger.info(f"initializing sqlite db path={settings.db_path}")

    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS org_profiles (
        org TEXT PRIMARY KEY,
        risk_tolerance TEXT DEFAULT 'medium',
        regulated_industry INTEGER DEFAULT 0,
        frameworks_json TEXT DEFAULT '["OWASP_LLM_TOP10","SOC2"]',
        review_threshold REAL DEFAULT 0.45,
        block_threshold REAL DEFAULT 0.75,
        memory_half_life_days INTEGER DEFAULT 180,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS changes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_hash TEXT UNIQUE,
        org TEXT,
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
        compliance_json TEXT,
        similar_memory_json TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS outcomes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_id INTEGER,
        incident INTEGER,
        severity TEXT,
        notes TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS senior_judgments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_id INTEGER,
        reviewer TEXT,
        label TEXT,
        confidence REAL,
        rationale TEXT,
        created_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS memory_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source TEXT,
        org TEXT,
        text TEXT,
        label TEXT,
        incident INTEGER,
        severity TEXT,
        notes TEXT,
        framework TEXT,
        created_at TEXT,
        expires_at TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS approvals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        change_id INTEGER,
        org TEXT,
        provider TEXT,
        external_id TEXT,
        status TEXT,
        requested_by TEXT,
        decided_by TEXT,
        reason TEXT,
        created_at TEXT,
        updated_at TEXT
    )
    """)

    conn.commit()
    conn.close()

def get_org_profile(org: str) -> Dict[str, Any]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM org_profiles WHERE org=?", (org,))
    row = cur.fetchone()

    if row:
        conn.close()
        return dict(row)

    profile = {
        "org": org,
        "risk_tolerance": "medium",
        "regulated_industry": 0,
        "frameworks_json": json.dumps(["OWASP_LLM_TOP10", "SOC2"]),
        "review_threshold": settings.review_threshold,
        "block_threshold": settings.block_threshold,
        "memory_half_life_days": settings.memory_half_life_days,
    }

    cur.execute("""
    INSERT INTO org_profiles (
        org, risk_tolerance, regulated_industry, frameworks_json,
        review_threshold, block_threshold, memory_half_life_days,
        created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        profile["org"],
        profile["risk_tolerance"],
        profile["regulated_industry"],
        profile["frameworks_json"],
        profile["review_threshold"],
        profile["block_threshold"],
        profile["memory_half_life_days"],
        utc_now(),
        utc_now(),
    ))

    conn.commit()
    conn.close()

    return profile

def upsert_org_profile(
    org: str,
    risk_tolerance: str,
    regulated_industry: bool,
    frameworks: List[str],
    review_threshold: float,
    block_threshold: float,
    memory_half_life_days: int,
):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO org_profiles (
        org, risk_tolerance, regulated_industry, frameworks_json,
        review_threshold, block_threshold, memory_half_life_days,
        created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(org) DO UPDATE SET
        risk_tolerance=excluded.risk_tolerance,
        regulated_industry=excluded.regulated_industry,
        frameworks_json=excluded.frameworks_json,
        review_threshold=excluded.review_threshold,
        block_threshold=excluded.block_threshold,
        memory_half_life_days=excluded.memory_half_life_days,
        updated_at=excluded.updated_at
    """, (
        org,
        risk_tolerance,
        1 if regulated_industry else 0,
        json.dumps(frameworks),
        review_threshold,
        block_threshold,
        memory_half_life_days,
        utc_now(),
        utc_now(),
    ))

    conn.commit()
    conn.close()

def change_hash(req: ChangeRequest) -> str:
    return stable_hash({
        "org": req.org,
        "repo": req.repo,
        "environment": req.environment,
        "change_type": req.change_type,
        "title": req.title,
        "diff": req.diff,
        "metadata": req.metadata,
    })

def save_change(req: ChangeRequest, result: Dict[str, Any]) -> int:
    conn = get_conn()
    cur = conn.cursor()
    h = change_hash(req)

    try:
        cur.execute("""
        INSERT INTO changes (
            change_hash, org, repo, author, environment, change_type,
            title, diff, metadata_json, risk_score, decision,
            reasons_json, compliance_json, similar_memory_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            h,
            req.org,
            req.repo,
            req.author,
            req.environment,
            req.change_type,
            req.title,
            req.diff,
            json.dumps(req.metadata),
            result["risk_score"],
            result["decision"],
            json.dumps(result["reasons"]),
            json.dumps(result["compliance_findings"]),
            json.dumps(result["similar_memory"]),
            utc_now(),
        ))

        change_id = cur.lastrowid
        conn.commit()

        logger.info(
            f"change_saved change_id={change_id} org={req.org} "
            f"decision={result['decision']} score={result['risk_score']}"
        )

    except sqlite3.IntegrityError:
        cur.execute("SELECT id FROM changes WHERE change_hash=?", (h,))
        row = cur.fetchone()
        change_id = row["id"]
        logger.info(f"duplicate_change change_id={change_id}")

    conn.close()
    return int(change_id)

def get_change(change_id: int) -> Optional[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("SELECT * FROM changes WHERE id=?", (change_id,))
    row = cur.fetchone()
    conn.close()

    return dict(row) if row else None

def list_changes(limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT id, org, repo, author, environment, change_type,
           title, risk_score, decision, created_at
    FROM changes
    ORDER BY created_at DESC
    LIMIT ?
    """, (limit,))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def create_memory_item(
    source: str,
    org: str,
    text: str,
    label: str,
    incident: bool,
    severity: str,
    notes: str,
    framework: str,
) -> int:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO memory_items (
        source, org, text, label, incident, severity,
        notes, framework, created_at, expires_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source,
        org,
        text,
        label,
        1 if incident else 0,
        severity,
        notes,
        framework,
        utc_now(),
        None,
    ))

    memory_id = cur.lastrowid
    conn.commit()
    conn.close()

    logger.info(
        f"memory_created memory_id={memory_id} source={source} "
        f"org={org} label={label} severity={severity}"
    )

    return int(memory_id)

def list_memory_items(limit: int = 100000) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    SELECT * FROM memory_items
    WHERE expires_at IS NULL
    ORDER BY id ASC
    LIMIT ?
    """, (limit,))

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def get_memory_items_by_ids(ids: List[int]) -> List[Dict[str, Any]]:
    if not ids:
        return []

    placeholders = ",".join(["?"] * len(ids))
    conn = get_conn()
    cur = conn.cursor()

    cur.execute(
        f"SELECT * FROM memory_items WHERE id IN ({placeholders})",
        ids,
    )

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()

    by_id = {r["id"]: r for r in rows}
    return [by_id[i] for i in ids if i in by_id]

def record_outcome(change_id: int, incident: bool, severity: str, notes: str):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO outcomes (change_id, incident, severity, notes, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (
        change_id,
        1 if incident else 0,
        severity,
        notes,
        utc_now(),
    ))

    conn.commit()
    conn.close()

    logger.info(
        f"outcome_recorded change_id={change_id} incident={incident} severity={severity}"
    )

def record_judgment(
    change_id: int,
    reviewer: str,
    label: str,
    confidence: float,
    rationale: str,
):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO senior_judgments (
        change_id, reviewer, label, confidence, rationale, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        change_id,
        reviewer,
        label,
        confidence,
        rationale,
        utc_now(),
    ))

    conn.commit()
    conn.close()

    logger.info(
        f"judgment_recorded change_id={change_id} reviewer={reviewer} label={label}"
    )

def create_approval(change_id: int, org: str, requested_by: str) -> int:
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    INSERT INTO approvals (
        change_id, org, provider, external_id, status,
        requested_by, decided_by, reason, created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        change_id,
        org,
        "internal",
        None,
        "pending",
        requested_by,
        None,
        "",
        utc_now(),
        utc_now(),
    ))

    approval_id = cur.lastrowid
    conn.commit()
    conn.close()

    logger.info(f"approval_created approval_id={approval_id} change_id={change_id}")
    return int(approval_id)

def list_approvals(status: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_conn()
    cur = conn.cursor()

    if status:
        cur.execute(
            "SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC",
            (status,),
        )
    else:
        cur.execute("SELECT * FROM approvals ORDER BY created_at DESC")

    rows = [dict(r) for r in cur.fetchall()]
    conn.close()
    return rows

def decide_approval(
    approval_id: int,
    status: str,
    decided_by: str,
    reason: str,
):
    conn = get_conn()
    cur = conn.cursor()

    cur.execute("""
    UPDATE approvals
    SET status=?, decided_by=?, reason=?, updated_at=?
    WHERE id=?
    """, (
        status,
        decided_by,
        reason,
        utc_now(),
        approval_id,
    ))

    conn.commit()
    conn.close()

    logger.info(
        f"approval_decided approval_id={approval_id} status={status} decided_by={decided_by}"
    )
