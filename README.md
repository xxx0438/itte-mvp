# ITTE MVP

ITTE is a self-evolving risk brain for AI engineering.

It intercepts high-risk AI changes before deployment and learns from:

- public AI risk data
- private incidents
- senior engineer judgments
- compliance findings
- stale memory decay

## Key MVP Features

- CI/CD risk gate
- Persistent FAISS memory index
- Incremental memory indexing
- Periodic background FAISS rebuild
- SQLite persistence
- Public cold-start risk memory
- Private incident memory
- Senior engineer judgment distillation
- Memory decay / metabolizing
- Compliance templates:
- OWASP LLM Top 10
- SOC2
- HIPAA
- EU AI Act
- Optional local open-source LLM judge
- Prometheus /metrics
- Structured logs via loguru

## Install

python -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env

## Start Server

uvicorn itte.main:app --reload --port 8000
http://localhost:8000/docs
curl http://localhost:8000/health
curl http://localhost:8000/metrics
