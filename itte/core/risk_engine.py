import json
from typing import Dict, List

from itte.api.schemas import ChangeRequest
from itte.core.heuristic import heuristic_score
from itte.core.compliance import compliance_check
from itte.core.llm_judge import llm_judge
from itte.memory.decay import decay_weight, label_risk_weight
from itte.observability import logger, MEMORY_SEARCH_LATENCY
from itte.db import repository as repo

class RiskEngine:
    def __init__(self, vector_store):
        self.vector_store = vector_store

    async def evaluate(self, req: ChangeRequest) -> Dict:
        logger.info(
            f"risk_evaluate_start org={req.org} repo={req.repo} "
            f"type={req.change_type} env={req.environment}"
        )

        org_profile = repo.get_org_profile(req.org)

        score, reasons = heuristic_score(req)

        compliance_findings = compliance_check(req, org_profile)

        if compliance_findings:
            compliance_score = min(
                0.35,
                sum(float(x["score"]) for x in compliance_findings),
            )
            score += compliance_score
            reasons.append(f"Compliance templates added risk: {round(compliance_score, 3)}.")

        similar_memory, memory_boost = await self._memory_risk(req, org_profile)

        if memory_boost > 0:
            score += memory_boost
            reasons.append(f"Metabolizing memory added risk: {round(memory_boost, 3)}.")

        llm_score, llm_reasons = llm_judge(
            req,
            compliance_findings,
            similar_memory,
        )

        if llm_score > 0:
            score = 0.7 * score + 0.3 * llm_score
            reasons.append(f"Open-source LLM judge risk score: {round(llm_score, 3)}.")
            reasons.extend(llm_reasons)

        if int(org_profile.get("regulated_industry", 0)):
            score += 0.05
            reasons.append("Organization is marked as regulated industry.")

        risk_tolerance = org_profile.get("risk_tolerance", "medium")

        if risk_tolerance == "low":
            score *= 1.15
            reasons.append("Organization has low risk tolerance.")
        elif risk_tolerance == "high":
            score *= 0.9
            reasons.append("Organization has high risk tolerance.")

        score = max(0.0, min(1.0, score))

        review_threshold = float(org_profile.get("review_threshold", 0.45))
        block_threshold = float(org_profile.get("block_threshold", 0.75))

        if score >= block_threshold:
            decision = "block"
            reasons.append("Risk score exceeds block threshold.")
        elif score >= review_threshold:
            decision = "review"
            reasons.append("Risk score exceeds review threshold.")
        else:
            decision = "allow"
            reasons.append("Risk score below review threshold.")

        result = {
            "risk_score": round(score, 3),
            "decision": decision,
            "reasons": reasons,
            "compliance_findings": compliance_findings,
            "similar_memory": similar_memory,
        }

        logger.info(
            f"risk_evaluate_complete org={req.org} decision={decision} "
            f"score={result['risk_score']} memory={len(similar_memory)}"
        )

        return result

    async def _memory_risk(self, req: ChangeRequest, org_profile: Dict):
        query = f"""
        {req.change_type}
        {req.title}
        {req.diff}
        {json.dumps(req.metadata)}
        """

        with MEMORY_SEARCH_LATENCY.time():
            hits = await self.vector_store.search(query, k=8)

        if not hits:
            return [], 0.0

        ids = [memory_id for memory_id, _score in hits]
        rows = repo.get_memory_items_by_ids(ids)

        by_id = {int(r["id"]): r for r in rows}

        similar = []
        boost = 0.0

        half_life_days = int(org_profile.get("memory_half_life_days", 180))

        for memory_id, similarity in hits:
            item = by_id.get(memory_id)
            if not item:
                continue

            if similarity < 0.25:
                continue

            decay = decay_weight(item["created_at"], half_life_days)

            risk_weight = label_risk_weight(
                item["label"],
                int(item["incident"]),
                item["severity"],
            )

            risk_boost = max(0.0, similarity * decay * risk_weight)

            similar.append({
                "memory_id": memory_id,
                "source": item["source"],
                "org": item["org"],
                "label": item["label"],
                "incident": bool(item["incident"]),
                "severity": item["severity"],
                "framework": item["framework"],
                "similarity": round(float(similarity), 3),
                "decay": round(float(decay), 3),
                "risk_boost": round(float(risk_boost), 3),
                "notes": item["notes"],
            })

            boost += risk_boost

        return similar[:5], min(0.35, boost)
