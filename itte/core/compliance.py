import json
import re
from typing import Dict, List

from itte.api.schemas import ChangeRequest

def compliance_check(req: ChangeRequest, org_profile: Dict) -> List[Dict]:
    metadata = req.metadata or {}
    text = f"{req.title}\n{req.diff}\n{json.dumps(metadata)}".lower()

    frameworks = metadata.get("frameworks")
    if not frameworks:
        frameworks = json.loads(org_profile.get("frameworks_json", "[]"))

    findings = []

    def add(framework: str, title: str, severity: str, score: float, reason: str):
        findings.append({
            "framework": framework,
            "title": title,
            "severity": severity,
            "score": score,
            "reason": reason,
        })

    if "OWASP_LLM_TOP10" in frameworks:
        if re.search(r"ignore\s+(all\s+)?previous|jailbreak|prompt injection", text):
            add(
                "OWASP_LLM_TOP10",
                "Prompt injection or instruction override risk",
                "high",
                0.22,
                "Change weakens instruction hierarchy or allows hostile instructions.",
            )

        if "auto approve" in text or "autonomous" in text or metadata.get("agent_can_execute"):
            add(
                "OWASP_LLM_TOP10",
                "Excessive agency risk",
                "high",
                0.20,
                "Agent may take external actions without enough approval.",
            )

        if any(x in text for x in ["secret", "token", "password", "credential", "pii", "customer data"]):
            add(
                "OWASP_LLM_TOP10",
                "Sensitive information disclosure risk",
                "medium",
                0.15,
                "Change touches credentials or sensitive user data.",
            )

    if "SOC2" in frameworks:
        if not metadata.get("rollback_plan"):
            add(
                "SOC2",
                "Missing rollback plan",
                "medium",
                0.08,
                "Production change lacks rollback evidence.",
            )

        if req.environment.lower() in ["prod", "production"] and not metadata.get("approval_ticket"):
            add(
                "SOC2",
                "Missing approval evidence",
                "medium",
                0.10,
                "Production change lacks approval ticket evidence.",
            )

    if "HIPAA" in frameworks:
        if metadata.get("touches_health_data") or "health data" in text:
            if not metadata.get("privacy_review"):
                add(
                    "HIPAA",
                    "Health data change without privacy review",
                    "high",
                    0.22,
                    "Potential PHI workflow requires privacy review.",
                )

    if "EU_AI_ACT" in frameworks:
        if metadata.get("high_risk_ai_system") and not metadata.get("risk_management_record"):
            add(
                "EU_AI_ACT",
                "Missing risk management record",
                "high",
                0.20,
                "High-risk AI system change lacks risk documentation.",
            )

        if metadata.get("automated_decisioning") and not metadata.get("human_oversight"):
            add(
                "EU_AI_ACT",
                "Missing human oversight",
                "high",
                0.20,
                "Automated decisioning lacks human oversight evidence.",
            )

    return findings
