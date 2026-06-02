import os
import re
import hmac
import json
import math
import time
import hashlib
import sqlite3
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import requests
import faiss
import joblib

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, Header
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

load_dotenv()

DB_PATH = os.getenv("ITTE_DB_PATH", "itte.db")
EMBED_MODEL_NAME = os.getenv("ITTE_EMBED_MODEL", "sentence-transformers/all-MiniLM-L6-v2")
USE_LLM = os.getenv("ITTE_USE_LLM", "0") == "1"
LLM_MODEL_NAME = os.getenv("ITTE_LLM_MODEL", "Qwen/Qwen2.5-Coder-1.5B-Instruct")

MODEL_PATH = "models/risk_model.joblib"

_embedder = None
_llm = None
_tokenizer = None
_private_model = None

# =========================
# Database
# =========================

def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    c = conn()
    cur = c.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS org_profiles (
        org TEXT PRIMARY KEY,
        risk_tolerance TEXT DEFAULT 'medium',
        regulated_industry INTEGER DEFAULT 0,
        default_frameworks_json TEXT DEFAULT '["OWASP_LLM_TOP10","SOC2"]',
        memory_half_life_days INTEGER DEFAULT 180,
        review_threshold REAL DEFAULT 0.45,
        block_threshold REAL DEFAULT 0.75,
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

    c.commit()
    c.close()

def now() -> str:
    return datetime.utcnow().isoformat()

# =========================
# Models
# =========================

class ChangeRequest(BaseModel):
    org: str = "default"
    repo: str
    author: str
    environment: str = "production"
    change_type: str = Field(..., description="prompt | config | model | tool | code | policy")
    title: str
    diff: str
    metadata: Dict[str, Any] = Field(default_factory=dict)

class RiskResponse(BaseModel):
    change_id: int
    risk_score: float
    decision: str
    reasons: List[str]
    compliance_findings: List[Dict[str, Any]]
    similar_memory: List[Dict[str, Any]]
    approval_id: Optional[int] = None

class OutcomeRequest(BaseModel):
    change_id: int
    incident: bool
    severity: str = "none"
    notes: str = ""

class JudgmentRequest(BaseModel):
    change_id: int
    reviewer: str
    label: str = Field(..., description="allow | review | block")
    confidence: float = 0.8
    rationale: str = ""

class OrgProfileRequest(BaseModel):
    risk_tolerance: str = "medium"
    regulated_industry: bool = False
    default_frameworks: List[str] = Field(default_factory=lambda: ["OWASP_LLM_TOP10", "SOC2"])
    memory_half_life_days: int = 180
    review_threshold: float = 0.45
    block_threshold: float = 0.75

class SeedMemoryRequest(BaseModel):
    source: str = "public"
    org: str = "global"
    text: str
    label: str = "review"
    incident: bool = False
    severity: str = "medium"
    notes: str = ""
    framework: str = "OWASP_LLM_TOP10"

class ApprovalDecisionRequest(BaseModel):
    status: str = Field(..., description="approved | rejected")
    decided_by: str
    reason: str = ""

# =========================
# Lazy Models
# =========================

def get_embedder():
    global _embedder
    if _embedder is None:
        _embedder = SentenceTransformer(EMBED_MODEL_NAME)
    return _embedder

def embed(texts: List[str]) -> np.ndarray:
    model = get_embedder()
    vecs = model.encode(texts, normalize_embeddings=True)
    return np.array(vecs, dtype="float32")

def load_private_model():
    global _private_model
    if _private_model is not None:
        return _private_model
    if os.path.exists(MODEL_PATH):
        _private_model = joblib.load(MODEL_PATH)
    return _private_model

def get_llm():
    global _llm, _tokenizer

    if not USE_LLM:
        return None, None

    if _llm is not None and _tokenizer is not None:
        return _llm, _tokenizer

    from transformers import AutoModelForCausalLM, AutoTokenizer
    import torch

    _tokenizer = AutoTokenizer.from_pretrained(LLM_MODEL_NAME)
    _llm = AutoModelForCausalLM.from_pretrained(
        LLM_MODEL_NAME,
        torch_dtype="auto",
        device_map="auto"
    )
    return _llm, _tokenizer

# =========================
# Org Profile
# =========================

def get_org_profile(org: str) -> Dict[str, Any]:
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM org_profiles WHERE org=?", (org,))
    row = cur.fetchone()

    if not row:
        profile = {
            "org": org,
            "risk_tolerance": "medium",
            "regulated_industry": 0,
            "default_frameworks_json": json.dumps(["OWASP_LLM_TOP10", "SOC2"]),
            "memory_half_life_days": 180,
            "review_threshold": 0.45,
            "block_threshold": 0.75,
        }
        cur.execute("""
        INSERT INTO org_profiles (
            org, risk_tolerance, regulated_industry, default_frameworks_json,
            memory_half_life_days, review_threshold, block_threshold,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            org,
            profile["risk_tolerance"],
            profile["regulated_industry"],
            profile["default_frameworks_json"],
            profile["memory_half_life_days"],
            profile["review_threshold"],
            profile["block_threshold"],
            now(),
            now()
        ))
        c.commit()
        c.close()
        return profile

    result = dict(row)
    c.close()
    return result

def risk_tolerance_multiplier(profile: Dict[str, Any]) -> float:
    rt = profile.get("risk_tolerance", "medium")
    if rt == "low":
        return 1.15
    if rt == "high":
        return 0.9
    return 1.0

# =========================
# Compliance Templates
# =========================

CRITICAL_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disable\s+(safety|moderation|guardrails?)",
    r"bypass\s+(policy|safety|approval)",
    r"auto[-\s]?approve",
    r"drop\s+table",
    r"truncate\s+table",
    r"export\s+.*(pii|personal data|customer data|health data)",
]

HIGH_RISK_KEYWORDS = [
    "production", "prod", "payment", "billing", "refund", "delete", "admin",
    "root", "secret", "token", "password", "credential", "pii", "gdpr",
    "hipaa", "compliance", "jailbreak", "ignore previous", "bypass",
    "disable safety", "no moderation", "autonomous", "auto approve",
    "execute", "shell", "database", "customer data", "personal data",
    "health data", "medical", "diagnosis", "insurance"
]

def compliance_check(req: ChangeRequest, profile: Dict[str, Any]) -> List[Dict[str, Any]]:
    metadata = req.metadata or {}
    text = f"{req.title}\n{req.diff}\n{json.dumps(metadata)}".lower()

    frameworks = metadata.get("frameworks")
    if not frameworks:
        frameworks = json.loads(profile["default_frameworks_json"])

    findings = []

    def add(framework, title, severity, score, reason):
        findings.append({
            "framework": framework,
            "title": title,
            "severity": severity,
            "score": score,
            "reason": reason
        })

    if "OWASP_LLM_TOP10" in frameworks:
        if re.search(r"ignore\s+(all\s+)?previous\s+instructions|jailbreak|prompt injection", text):
            add(
                "OWASP_LLM_TOP10",
                "Prompt injection or instruction override risk",
                "high",
                0.22,
                "Change appears to weaken instruction hierarchy or accept hostile instructions."
            )

        if "auto approve" in text or "autonomous" in text or metadata.get("agent_can_execute"):
            add(
                "OWASP_LLM_TOP10",
                "Excessive agency risk",
                "high",
                0.2,
                "Agent may take actions without enough human approval."
            )

        if any(x in text for x in ["secret", "token", "password", "credential", "pii", "customer data"]):
            add(
                "OWASP_LLM_TOP10",
                "Sensitive information disclosure risk",
                "medium",
                0.15,
                "Change touches secrets, credentials, or sensitive user data."
            )

        if req.change_type in ["model", "tool"] or metadata.get("new_dependency"):
            add(
                "OWASP_LLM_TOP10",
                "Supply chain or tool integration risk",
                "medium",
                0.12,
                "Model/tool/dependency changes may introduce supply-chain exposure."
            )

    if "SOC2" in frameworks:
        if not metadata.get("rollback_plan"):
            add(
                "SOC2",
                "Missing rollback plan",
                "medium",
                0.08,
                "Production change lacks rollback evidence."
            )

        if req.environment.lower() in ["prod", "production"] and not metadata.get("approval_ticket"):
            add(
                "SOC2",
                "Missing approval evidence",
                "medium",
                0.1,
                "Production change lacks approval ticket evidence."
            )

    if "HIPAA" in frameworks:
        if metadata.get("touches_health_data") or any(x in text for x in ["hipaa", "health data", "medical", "diagnosis"]):
            if not metadata.get("privacy_review"):
                add(
                    "HIPAA",
                    "Health data change without privacy review",
                    "high",
                    0.22,
                    "Potential PHI workflow requires privacy review evidence."
                )

    if "EU_AI_ACT" in frameworks:
        if metadata.get("high_risk_ai_system") and not metadata.get("risk_management_record"):
            add(
                "EU_AI_ACT",
                "Missing risk management record",
                "high",
                0.2,
                "High-risk AI system change lacks risk management documentation."
            )

        if metadata.get("automated_decisioning") and not metadata.get("human_oversight"):
            add(
                "EU_AI_ACT",
                "Missing human oversight",
                "high",
                0.2,
                "Automated decisioning requires human oversight evidence."
            )

    return findings

# =========================
# Memory / Vector Search
# =========================

def decay_weight(created_at: str, half_life_days: int) -> float:
    try:
        dt = datetime.fromisoformat(created_at)
        age_days = max(0, (datetime.utcnow() - dt).days)
        return 0.5 ** (age_days / max(1, half_life_days))
    except Exception:
        return 1.0

def label_weight(label: str, incident: int, severity: str) -> float:
    base = {
        "allow": -0.05,
        "review": 0.12,
        "block": 0.25,
    }.get(label, 0.1)

    sev = {
        "none": 0.0,
        "low": 0.05,
        "medium": 0.12,
        "high": 0.22,
        "critical": 0.35,
    }.get((severity or "").lower(), 0.08)

    if incident:
        return base + sev + 0.1
    return base + sev

def search_memory(req: ChangeRequest, profile: Dict[str, Any], k: int = 5) -> Tuple[List[Dict[str, Any]], float]:
    c = conn()
    cur = c.cursor()

    cur.execute("""
    SELECT * FROM memory_items
    WHERE org IN (?, 'global')
    ORDER BY created_at DESC
    LIMIT 1000
    """, (req.org,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()

    if not rows:
        return [], 0.0

    query_text = f"{req.change_type}\n{req.title}\n{req.diff}\n{json.dumps(req.metadata)}"
    texts = [r["text"] for r in rows]

    qv = embed([query_text])
    xv = embed(texts)

    index = faiss.IndexFlatIP(xv.shape[1])
    index.add(xv)

    scores, ids = index.search(qv, min(k, len(rows)))

    similar = []
    boost = 0.0
    half_life = int(profile["memory_half_life_days"])

    for sim, idx in zip(scores[0], ids[0]):
        if idx < 0:
            continue

        r = rows[idx]
        if sim < 0.25:
            continue

        decay = decay_weight(r["created_at"], half_life)
        weighted = float(sim) * decay * label_weight(r["label"], r["incident"], r["severity"])

        similar.append({
            "memory_id": r["id"],
            "source": r["source"],
            "org": r["org"],
            "label": r["label"],
            "incident": bool(r["incident"]),
            "severity": r["severity"],
            "framework": r["framework"],
            "similarity": round(float(sim), 3),
            "decay": round(decay, 3),
            "risk_boost": round(weighted, 3),
            "notes": r["notes"]
        })

        boost += max(0, weighted)

    return similar, min(0.35, boost)

# =========================
# Private ML Risk Model
# =========================

def private_model_score(req: ChangeRequest) -> Tuple[float, List[str]]:
    pack = load_private_model()
    if not pack:
        return 0.0, []

    text = f"{req.environment} {req.change_type} {req.title}\n{req.diff}\n{json.dumps(req.metadata)}"
    vec = pack["vectorizer"].transform([text])
    clf = pack["classifier"]
    probs = clf.predict_proba(vec)[0]
    classes = list(clf.classes_)

    risk = 0.0
    if "block" in classes:
        risk += probs[classes.index("block")] * 0.9
    if "review" in classes:
        risk += probs[classes.index("review")] * 0.55

    reason = f"Private distilled risk model estimated risk contribution: {risk:.3f}."
    return float(risk), [reason]

# =========================
# Local LLM Judge
# =========================

def llm_judge(req: ChangeRequest, compliance: List[Dict[str, Any]], similar: List[Dict[str, Any]]) -> Tuple[float, List[str]]:
    if not USE_LLM:
        return 0.0, []

    model, tokenizer = get_llm()
    if model is None:
        return 0.0, []

    prompt = f"""
You are ITTE, a pre-deployment risk judge for AI engineering changes.

Return strict JSON:
{{
  "risk_score": number between 0 and 1,
  "reasons": ["short reason 1", "short reason 2"]
}}

Change:
org={req.org}
repo={req.repo}
environment={req.environment}
change_type={req.change_type}
title={req.title}
metadata={json.dumps(req.metadata)}

diff:
{req.diff[:6000]}

Compliance findings:
{json.dumps(compliance[:8])}

Similar memory:
{json.dumps(similar[:5])}
"""

    messages = [
        {"role": "system", "content": "You are a careful AI deployment risk reviewer."},
        {"role": "user", "content": prompt}
    ]

    try:
        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True
        ).to(model.device)

        output = model.generate(**inputs, max_new_tokens=220, do_sample=False)
        text = tokenizer.decode(
            output[0] [inputs["input_ids"].shape[-1]:],
            skip_special_tokens=True
        )

        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            return 0.0, ["LLM judge returned non-JSON output."]

        data = json.loads(m.group(0))
        score = float(data.get("risk_score", 0))
        reasons = data.get("reasons", [])
        return max(0.0, min(1.0, score)), [f"LLM: {r}" for r in reasons[:5]]

    except Exception as e:
        return 0.0, [f"LLM judge failed: {str(e)}"]

# =========================
# Core Risk Engine
# =========================

def hash_change(req: ChangeRequest) -> str:
    raw = json.dumps({
        "org": req.org,
        "repo": req.repo,
        "environment": req.environment,
        "change_type": req.change_type,
        "title": req.title,
        "diff": req.diff,
        "metadata": req.metadata
    }, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()

def heuristic_score(req: ChangeRequest) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []

    text = f"{req.repo} {req.environment} {req.change_type} {req.title}\n{req.diff}\n{json.dumps(req.metadata)}".lower()

    if req.environment.lower() in ["prod", "production"]:
        score += 0.2
        reasons.append("Change targets production environment.")

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
        reasons.append(f"Change type `{req.change_type}` is high impact.")

    hits = sorted({kw for kw in HIGH_RISK_KEYWORDS if kw in text})
    if hits:
        add = min(0.25, 0.03 * len(hits))
        score += add
        reasons.append("High-risk terms detected: " + ", ".join(hits[:12]))

    pattern_hits = [p for p in CRITICAL_PATTERNS if re.search(p, text)]
    if pattern_hits:
        score += 0.35
        reasons.append("Critical unsafe pattern detected.")

    lines = len(req.diff.splitlines())
    if lines > 100:
        score += 0.15
        reasons.append(f"Large change detected: {lines} lines.")
    elif lines > 30:
        score += 0.08
        reasons.append(f"Medium-sized change detected: {lines} lines.")

    md = req.metadata or {}
    if md.get("touches_customer_data"):
        score += 0.22
        reasons.append("Metadata says change touches customer data.")

    if md.get("touches_health_data"):
        score += 0.25
        reasons.append("Metadata says change touches health data.")

    if md.get("agent_can_execute"):
        score += 0.18
        reasons.append("Agent can execute tools or external actions.")

    if not md.get("rollback_plan"):
        score += 0.08
        reasons.append("No rollback plan found.")

    return min(1.0, score), reasons

def save_change(req: ChangeRequest, result: Dict[str, Any]) -> int:
    c = conn()
    cur = c.cursor()
    h = hash_change(req)

    try:
        cur.execute("""
        INSERT INTO changes (
            change_hash, org, repo, author, environment, change_type,
            title, diff, metadata_json, risk_score, decision,
            reasons_json, compliance_json, similar_memory_json, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            h, req.org, req.repo, req.author, req.environment, req.change_type,
            req.title, req.diff, json.dumps(req.metadata),
            result["risk_score"], result["decision"],
            json.dumps(result["reasons"]),
            json.dumps(result["compliance_findings"]),
            json.dumps(result["similar_memory"]),
            now()
        ))
        change_id = cur.lastrowid
        c.commit()
    except sqlite3.IntegrityError:
        cur.execute("SELECT id FROM changes WHERE change_hash=?", (h,))
        change_id = cur.fetchone()["id"]

    c.close()
    return change_id

def create_memory_item(
    source: str,
    org: str,
    text: str,
    label: str,
    incident: bool,
    severity: str,
    notes: str,
    framework: str = ""
):
    c = conn()
    cur = c.cursor()
    cur.execute("""
    INSERT INTO memory_items (
        source, org, text, label, incident, severity,
        notes, framework, created_at, expires_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        source, org, text, label, 1 if incident else 0,
        severity, notes, framework, now(), None
    ))
    c.commit()
    c.close()

def evaluate(req: ChangeRequest) -> Dict[str, Any]:
    profile = get_org_profile(req.org)

    score, reasons = heuristic_score(req)

    compliance = compliance_check(req, profile)
    if compliance:
        c_score = min(0.35, sum(float(f["score"]) for f in compliance))
        score += c_score
        reasons.append(f"Compliance templates added risk: {round(c_score, 3)}.")

    similar, memory_boost = search_memory(req, profile)
    if memory_boost > 0:
        score += memory_boost
        reasons.append(f"Metabolizing memory added risk: {round(memory_boost, 3)}.")

    ml_score, ml_reasons = private_model_score(req)
    if ml_score > 0:
        score = max(score, ml_score)
        reasons.extend(ml_reasons)

    llm_score, llm_reasons = llm_judge(req, compliance, similar)
    if llm_score > 0:
        score = 0.7 * score + 0.3 * llm_score
        reasons.append(f"Open-source LLM judge risk score: {round(llm_score, 3)}.")
        reasons.extend(llm_reasons)

    score *= risk_tolerance_multiplier(profile)

    if int(profile["regulated_industry"]):
        score += 0.05
        reasons.append("Organization is marked as regulated industry.")

    score = max(0.0, min(1.0, score))

    review_threshold = float(profile["review_threshold"])
    block_threshold = float(profile["block_threshold"])

    if score >= block_threshold:
        decision = "block"
        reasons.append("Risk score exceeds block threshold.")
    elif score >= review_threshold:
        decision = "review"
        reasons.append("Risk score exceeds review threshold.")
    else:
        decision = "allow"
        reasons.append("Risk score below review threshold.")

    return {
        "risk_score": round(score, 3),
        "decision": decision,
        "reasons": reasons,
        "compliance_findings": compliance,
        "similar_memory": similar
    }

# =========================
# Approval Integrations
# =========================

def create_approval(change_id: int, org: str, requested_by: str) -> int:
    provider = os.getenv("APPROVAL_PROVIDER", "internal")

    c = conn()
    cur = c.cursor()
    cur.execute("""
    INSERT INTO approvals (
        change_id, org, provider, external_id, status,
        requested_by, decided_by, reason, created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        change_id, org, provider, None, "pending",
        requested_by, None, "", now(), now()
    ))
    approval_id = cur.lastrowid
    c.commit()
    c.close()

    external_id = notify_approval_provider(provider, approval_id, change_id, org)

    if external_id:
        c = conn()
        cur = c.cursor()
        cur.execute(
            "UPDATE approvals SET external_id=?, updated_at=? WHERE id=?",
            (external_id, now(), approval_id)
        )
        c.commit()
        c.close()

    return approval_id

def notify_approval_provider(provider: str, approval_id: int, change_id: int, org: str) -> Optional[str]:
    if provider == "jira":
        return create_jira_ticket(approval_id, change_id, org)
    if provider == "linear":
        return create_linear_issue(approval_id, change_id, org)
    return None

def create_jira_ticket(approval_id: int, change_id: int, org: str) -> Optional[str]:
    base = os.getenv("JIRA_BASE_URL")
    email = os.getenv("JIRA_EMAIL")
    token = os.getenv("JIRA_API_TOKEN")
    project = os.getenv("JIRA_PROJECT_KEY")

    if not all([base, email, token, project]):
        return None

    url = f"{base.rstrip('/')}/rest/api/3/issue"
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": f"ITTE review required: change #{change_id}",
            "description": {
                "type": "doc",
                "version": 1,
                "content": [{
                    "type": "paragraph",
                    "content": [{
                        "type": "text",
                        "text": f"ITTE requires human review. approval_id={approval_id}, org={org}, change_id={change_id}"
                    }]
                }]
            },
            "issuetype": {"name": "Task"}
        }
    }

    r = requests.post(url, json=payload, auth=(email, token), timeout=20)
    if r.status_code >= 300:
        return None
    return r.json().get("key")

def create_linear_issue(approval_id: int, change_id: int, org: str) -> Optional[str]:
    key = os.getenv("LINEAR_API_KEY")
    team_id = os.getenv("LINEAR_TEAM_ID")

    if not all([key, team_id]):
        return None

    query = """
    mutation IssueCreate($input: IssueCreateInput!) {
      issueCreate(input: $input) {
        success
        issue { id identifier }
      }
    }
    """

    variables = {
        "input": {
            "teamId": team_id,
            "title": f"ITTE review required: change #{change_id}",
            "description": f"approval_id={approval_id}, org={org}, change_id={change_id}"
        }
    }

    r = requests.post(
        "https://api.linear.app/graphql",
        json={"query": query, "variables": variables},
        headers={"Authorization": key},
        timeout=20
    )

    if r.status_code >= 300:
        return None

    data = r.json()
    issue = data.get("data", {}).get("issueCreate", {}).get("issue", {})
    return issue.get("identifier") or issue.get("id")

# =========================
# Webhook Verification
# =========================

def verify_github_signature(secret: str, body: bytes, sig: Optional[str]):
    if not secret:
        return True
    if not sig or not sig.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig)

# =========================
# FastAPI
# =========================

app = FastAPI(
    title="ITTE MVP",
    version="0.2.0",
    description="Self-evolving risk brain for AI engineering."
)

@app.on_event("startup")
def startup():
    init_db()

@app.get("/health")
def health():
    return {
        "ok": True,
        "service": "itte-mvp",
        "time": now(),
        "llm_enabled": USE_LLM,
        "embed_model": EMBED_MODEL_NAME
    }

@app.post("/risk/evaluate", response_model=RiskResponse)
def risk_evaluate(req: ChangeRequest):
    result = evaluate(req)
    change_id = save_change(req, result)

    approval_id = None
    if result["decision"] == "review":
        approval_id = create_approval(change_id, req.org, req.author)

    return RiskResponse(
        change_id=change_id,
        risk_score=result["risk_score"],
        decision=result["decision"],
        reasons=result["reasons"],
        compliance_findings=result["compliance_findings"],
        similar_memory=result["similar_memory"],
        approval_id=approval_id
    )

@app.post("/outcomes")
def record_outcome(req: OutcomeRequest):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM changes WHERE id=?", (req.change_id,))
    row = cur.fetchone()

    if not row:
        c.close()
        raise HTTPException(status_code=404, detail="Change not found")

    cur.execute("""
    INSERT INTO outcomes (change_id, incident, severity, notes, created_at)
    VALUES (?, ?, ?, ?, ?)
    """, (
        req.change_id,
        1 if req.incident else 0,
        req.severity,
        req.notes,
        now()
    ))
    c.commit()
    c.close()

    change = dict(row)
    text = f"{change['change_type']}\n{change['title']}\n{change['diff']}"
    label = "block" if req.incident and req.severity in ["high", "critical"] else "review"

    create_memory_item(
        source="private_incident",
        org=change["org"],
        text=text,
        label=label,
        incident=req.incident,
        severity=req.severity,
        notes=req.notes,
        framework="OUTCOME"
    )

    return {"ok": True, "message": "Outcome recorded and memory updated."}

@app.post("/judgments")
def record_judgment(req: JudgmentRequest):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM changes WHERE id=?", (req.change_id,))
    row = cur.fetchone()

    if not row:
        c.close()
        raise HTTPException(status_code=404, detail="Change not found")

    cur.execute("""
    INSERT INTO senior_judgments (
        change_id, reviewer, label, confidence, rationale, created_at
    )
    VALUES (?, ?, ?, ?, ?, ?)
    """, (
        req.change_id,
        req.reviewer,
        req.label,
        req.confidence,
        req.rationale,
        now()
    ))
    c.commit()
    c.close()

    change = dict(row)
    text = f"{change['change_type']}\n{change['title']}\n{change['diff']}\nSenior rationale: {req.rationale}"

    create_memory_item(
        source="senior_judgment",
        org=change["org"],
        text=text,
        label=req.label,
        incident=False,
        severity="medium" if req.label == "review" else "high" if req.label == "block" else "none",
        notes=f"{req.reviewer}: {req.rationale}",
        framework="JUDGMENT_DISTILLATION"
    )

    return {"ok": True, "message": "Senior judgment distilled into memory."}

@app.post("/memory/seed")
def seed_memory(req: SeedMemoryRequest):
    create_memory_item(
        source=req.source,
        org=req.org,
        text=req.text,
        label=req.label,
        incident=req.incident,
        severity=req.severity,
        notes=req.notes,
        framework=req.framework
    )
    return {"ok": True}

@app.get("/memory/search")
def memory_search(org: str, q: str, k: int = 5):
    fake = ChangeRequest(
        org=org,
        repo="search",
        author="system",
        environment="production",
        change_type="search",
        title=q,
        diff=q,
        metadata={}
    )
    profile = get_org_profile(org)
    similar, boost = search_memory(fake, profile, k)
    return {"risk_boost": boost, "items": similar}

@app.get("/changes")
def list_changes(limit: int = 50):
    c = conn()
    cur = c.cursor()
    cur.execute("""
    SELECT id, org, repo, author, environment, change_type,
           title, risk_score, decision, created_at
    FROM changes
    ORDER BY created_at DESC
    LIMIT ?
    """, (limit,))
    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

@app.get("/changes/{change_id}")
def get_change(change_id: int):
    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM changes WHERE id=?", (change_id,))
    row = cur.fetchone()

    if not row:
        c.close()
        raise HTTPException(status_code=404, detail="Change not found")

    result = dict(row)
    result["metadata"] = json.loads(result.pop("metadata_json"))
    result["reasons"] = json.loads(result.pop("reasons_json"))
    result["compliance_findings"] = json.loads(result.pop("compliance_json"))
    result["similar_memory"] = json.loads(result.pop("similar_memory_json"))

    cur.execute("SELECT * FROM outcomes WHERE change_id=? ORDER BY created_at DESC", (change_id,))
    result["outcomes"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM senior_judgments WHERE change_id=? ORDER BY created_at DESC", (change_id,))
    result["judgments"] = [dict(r) for r in cur.fetchall()]

    cur.execute("SELECT * FROM approvals WHERE change_id=? ORDER BY created_at DESC", (change_id,))
    result["approvals"] = [dict(r) for r in cur.fetchall()]

    c.close()
    return result

@app.get("/orgs/{org}/profile")
def get_profile(org: str):
    p = get_org_profile(org)
    p["default_frameworks"] = json.loads(p["default_frameworks_json"])
    return p

@app.post("/orgs/{org}/profile")
def set_profile(org: str, req: OrgProfileRequest):
    c = conn()
    cur = c.cursor()
    cur.execute("""
    INSERT INTO org_profiles (
        org, risk_tolerance, regulated_industry, default_frameworks_json,
        memory_half_life_days, review_threshold, block_threshold,
        created_at, updated_at
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(org) DO UPDATE SET
        risk_tolerance=excluded.risk_tolerance,
        regulated_industry=excluded.regulated_industry,
        default_frameworks_json=excluded.default_frameworks_json,
        memory_half_life_days=excluded.memory_half_life_days,
        review_threshold=excluded.review_threshold,
        block_threshold=excluded.block_threshold,
        updated_at=excluded.updated_at
    """, (
        org,
        req.risk_tolerance,
        1 if req.regulated_industry else 0,
        json.dumps(req.default_frameworks),
        req.memory_half_life_days,
        req.review_threshold,
        req.block_threshold,
        now(),
        now()
    ))
    c.commit()
    c.close()
    return {"ok": True}

@app.get("/approvals")
def list_approvals(status: Optional[str] = None):
    c = conn()
    cur = c.cursor()

    if status:
        cur.execute("SELECT * FROM approvals WHERE status=? ORDER BY created_at DESC", (status,))
    else:
        cur.execute("SELECT * FROM approvals ORDER BY created_at DESC")

    rows = [dict(r) for r in cur.fetchall()]
    c.close()
    return rows

@app.post("/approvals/{approval_id}/decision")
def decide_approval(approval_id: int, req: ApprovalDecisionRequest):
    if req.status not in ["approved", "rejected"]:
        raise HTTPException(status_code=400, detail="status must be approved or rejected")

    c = conn()
    cur = c.cursor()
    cur.execute("SELECT * FROM approvals WHERE id=?", (approval_id,))
    row = cur.fetchone()

    if not row:
        c.close()
        raise HTTPException(status_code=404, detail="Approval not found")

    cur.execute("""
    UPDATE approvals
    SET status=?, decided_by=?, reason=?, updated_at=?
    WHERE id=?
    """, (
        req.status,
        req.decided_by,
        req.reason,
        now(),
        approval_id
    ))

    c.commit()
    c.close()
    return {"ok": True}

@app.post("/webhooks/github")
async def github_webhook(
    request: Request,
    x_hub_signature_256: Optional[str] = Header(None),
    x_github_event: Optional[str] = Header(None)
):
    body = await request.body()
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    if not verify_github_signature(secret, body, x_hub_signature_256):
        raise HTTPException(status_code=401, detail="Invalid GitHub signature")

    payload = json.loads(body.decode())

    if x_github_event == "pull_request":
        pr = payload.get("pull_request", {})
        repo = payload.get("repository", {}).get("full_name", "unknown")
        author = pr.get("user", {}).get("login", "unknown")
        title = pr.get("title", "")
        diff = f"""
GitHub PR #{pr.get("number")}
title: {title}
body: {pr.get("body", "")}
base: {pr.get("base", {}).get("ref")}
head: {pr.get("head", {}).get("ref")}
"""
        req = ChangeRequest(
            org=payload.get("organization", {}).get("login", "default"),
            repo=repo,
            author=author,
            environment="production",
            change_type="code",
            title=title,
            diff=diff,
            metadata={
                "source": "github",
                "url": pr.get("html_url"),
                "rollback_plan": "Revert pull request"
            }
        )
        return risk_evaluate(req)

    return {"ok": True, "message": "Ignored GitHub event."}

@app.post("/webhooks/gitlab")
async def gitlab_webhook(
    request: Request,
    x_gitlab_token: Optional[str] = Header(None)
):
    expected = os.getenv("GITLAB_WEBHOOK_TOKEN", "")
    if expected and x_gitlab_token != expected:
        raise HTTPException(status_code=401, detail="Invalid GitLab token")

    payload = await request.json()
    kind = payload.get("object_kind")

    if kind == "merge_request":
        obj = payload.get("object_attributes", {})
        project = payload.get("project", {})
        user = payload.get("user", {})

        diff = f"""
GitLab MR !{obj.get("iid")}
title: {obj.get("title")}
description: {obj.get("description")}
source: {obj.get("source_branch")}
target: {obj.get("target_branch")}
"""

        req = ChangeRequest(
            org=payload.get("group", {}).get("name", "default"),
            repo=project.get("path_with_namespace", "unknown"),
            author=user.get("username", "unknown"),
            environment="production",
            change_type="code",
            title=obj.get("title", ""),
            diff=diff,
            metadata={
                "source": "gitlab",
                "url": obj.get("url"),
                "rollback_plan": "Revert merge request"
            }
        )
        return risk_evaluate(req)

    return {"ok": True, "message": "Ignored GitLab event."}
