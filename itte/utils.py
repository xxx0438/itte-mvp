import json
import hashlib
from datetime import datetime

def utc_now() -> str:
    return datetime.utcnow().isoformat()

def stable_hash(obj: dict) -> str:
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()

def safe_json_loads(value: str, default):
    try:
        return json.loads(value)
    except Exception:
        return default
