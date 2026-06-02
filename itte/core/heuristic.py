import json
import re
from typing import List, Tuple

from itte.api.schemas import ChangeRequest

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
    "health data",
]

CRITICAL_PATTERNS = [
    r"ignore\s+(all\s+)?previous\s+instructions",
    r"disable\s+(safety|moderation|guardrails?)",
    r"bypass\s+(policy|safety|approval)",
    r"auto[-\s]?approve",
    r"drop\s+table",
    r"truncate\s+table",
    r"export\s+.*(pii|personal data|customer data|health data)",
]

def heuristic_score(req: ChangeRequest) -> Tuple[float, List[str]]:
    score = 0.0
    reasons = []

    text = f"""
    {req.repo}
    {req.environment}
    {req.change_type}
    {req.title}
    {req.diff}
    {json.dumps(req.metadata)}
    """.lower()

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

    if any(re.search(pattern, text) for pattern in CRITICAL_PATTERNS):
        score += 0.35
        reasons.append("Critical unsafe pattern detected.")

    line_count = len(req.diff.splitlines())

    if line_count > 100:
        score += 0.15
        reasons.append(f"Large change detected: {line_count} lines.")
    elif line_count > 30:
        score += 0.08
        reasons.append(f"Medium-sized change detected: {line_count} lines.")

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
