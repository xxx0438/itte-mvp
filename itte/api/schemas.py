from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field

class ChangeRequest(BaseModel):
    org: str = "default"
    repo: str
    author: str
    environment: str = "production"
    change_type: str = Field(
        ...,
        description="prompt | config | model | tool | code | policy",
    )
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
    severity: str = Field("none", description="none | low | medium | high | critical")
    notes: str = ""

class JudgmentRequest(BaseModel):
    change_id: int
    reviewer: str
    label: str = Field(..., description="allow | review | block")
    confidence: float = 0.8
    rationale: str = ""

class MemorySeedRequest(BaseModel):
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

class OrgProfileRequest(BaseModel):
    risk_tolerance: str = "medium"
    regulated_industry: bool = False
    frameworks: List[str] = Field(default_factory=lambda: ["OWASP_LLM_TOP10", "SOC2"])
    review_threshold: float = 0.45
    block_threshold: float = 0.75
    memory_half_life_days: int = 180
