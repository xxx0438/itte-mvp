import sys
from loguru import logger
from prometheus_client import Counter, Histogram, Gauge

EVALUATE_COUNTER = Counter(
    "itte_evaluate_total",
    "Total number of risk evaluations",
    ["decision"],
)

EVALUATE_LATENCY = Histogram(
    "itte_evaluate_latency_seconds",
    "Risk evaluation latency in seconds",
)

MEMORY_SEARCH_LATENCY = Histogram(
    "itte_memory_search_latency_seconds",
    "Memory vector search latency in seconds",
)

MEMORY_ITEMS_GAUGE = Gauge(
    "itte_memory_items_total",
    "Number of memory items loaded in vector index",
)

LLM_COUNTER = Counter(
    "itte_llm_judge_total",
    "LLM judge invocations",
    ["status"],
)

def configure_logging(level: str = "INFO"):
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        serialize=False,
        backtrace=True,
        diagnose=False,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level}</level> | "
            "{name}:{function}:{line} | "
            "{message}"
        ),
    )

__all__ = [
    "logger",
    "configure_logging",
    "EVALUATE_COUNTER",
    "EVALUATE_LATENCY",
    "MEMORY_SEARCH_LATENCY",
    "MEMORY_ITEMS_GAUGE",
    "LLM_COUNTER",
]
