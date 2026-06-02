import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

@dataclass(frozen=True)
class Settings:
    db_path: str = os.getenv("ITTE_DB_PATH", "itte.db")

    index_dir: str = os.getenv("ITTE_INDEX_DIR", ".itte_index")
    embed_model: str = os.getenv(
        "ITTE_EMBED_MODEL",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    memory_rebuild_interval_seconds: int = int(
        os.getenv("ITTE_MEMORY_REBUILD_INTERVAL_SECONDS", "900")
    )

    use_llm: bool = os.getenv("ITTE_USE_LLM", "0") == "1"
    llm_model: str = os.getenv(
        "ITTE_LLM_MODEL",
        "Qwen/Qwen2.5-Coder-1.5B-Instruct",
    )

    log_level: str = os.getenv("ITTE_LOG_LEVEL", "INFO")

    review_threshold: float = float(os.getenv("ITTE_REVIEW_THRESHOLD", "0.45"))
    block_threshold: float = float(os.getenv("ITTE_BLOCK_THRESHOLD", "0.75"))
    memory_half_life_days: int = int(os.getenv("ITTE_MEMORY_HALF_LIFE_DAYS", "180"))

settings = Settings()
