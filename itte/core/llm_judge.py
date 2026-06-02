import json
import re
from typing import Dict, List, Tuple

from itte.config import settings
from itte.api.schemas import ChangeRequest
from itte.observability import logger, LLM_COUNTER

_model = None
_tokenizer = None

def _load_llm():
    global _model, _tokenizer

    if not settings.use_llm:
        return None, None

    if _model is not None and _tokenizer is not None:
        return _model, _tokenizer

    from transformers import AutoModelForCausalLM, AutoTokenizer

    logger.info(f"loading_llm model={settings.llm_model}")

    _tokenizer = AutoTokenizer.from_pretrained(settings.llm_model)
    _model = AutoModelForCausalLM.from_pretrained(
        settings.llm_model,
        torch_dtype="auto",
        device_map="auto",
    )

    return _model, _tokenizer

def llm_judge(
    req: ChangeRequest,
    compliance_findings: List[Dict],
    similar_memory: List[Dict],
) -> Tuple[float, List[str]]:
    if not settings.use_llm:
        return 0.0, []

    try:
        model, tokenizer = _load_llm()

        prompt = f"""
You are ITTE, a pre-deployment AI risk judge.

Return strict JSON only:
{{
  "risk_score": number between 0 and 1,
  "reasons": ["reason 1", "reason 2"]
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
{json.dumps(compliance_findings[:8])}

Similar memory:
{json.dumps(similar_memory[:5])}
"""

        messages = [
            {"role": "system", "content": "You are a careful AI deployment risk reviewer."},
            {"role": "user", "content": prompt},
        ]

        inputs = tokenizer.apply_chat_template(
            messages,
            tokenize=True,
            add_generation_prompt=True,
            return_tensors="pt",
            return_dict=True,
        ).to(model.device)

        output = model.generate(
            **inputs,
            max_new_tokens=220,
            do_sample=False,
        )

        generated = tokenizer.decode(
            output[0][inputs["input_ids"].shape[-1]:],
            skip_special_tokens=True,
        )

        match = re.search(r"\{.*\}", generated, re.S)
        if not match:
            LLM_COUNTER.labels(status="bad_output").inc()
            return 0.0, ["LLM judge returned non-JSON output."]

        data = json.loads(match.group(0))
        score = float(data.get("risk_score", 0.0))
        reasons = data.get("reasons", [])

        LLM_COUNTER.labels(status="success").inc()

        return max(0.0, min(1.0, score)), [
            f"LLM: {r}" for r in reasons[:5]
        ]

    except Exception as e:
        logger.exception(f"llm_judge_failed error={e}")
        LLM_COUNTER.labels(status="error").inc()
        return 0.0, [f"LLM judge failed: {str(e)}"]
