from datetime import datetime

def decay_weight(created_at: str, half_life_days: int) -> float:
    try:
        dt = datetime.fromisoformat(created_at)
        age_days = max(0, (datetime.utcnow() - dt).days)
        return 0.5 ** (age_days / max(1, half_life_days))
    except Exception:
        return 1.0

def label_risk_weight(label: str, incident: int, severity: str) -> float:
    base = {
        "allow": -0.05,
        "review": 0.12,
        "block": 0.25,
    }.get((label or "").lower(), 0.08)

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
