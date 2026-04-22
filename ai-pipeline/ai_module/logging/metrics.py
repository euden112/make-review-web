from __future__ import annotations

import functools
import logging
import time
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger("ai.metrics")


@dataclass(slots=True)
class TokenUsage:
    input_tokens: int
    output_tokens: int
    cached: bool
    model_name: str

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


class MetricsRegistry:
    def __init__(self) -> None:
        self.cache_hit = 0
        self.cache_miss = 0
        self.map_calls = 0
        self.reduce_calls = 0

    def record_cache(self, hit: bool) -> None:
        if hit:
            self.cache_hit += 1
        else:
            self.cache_miss += 1

    def cache_hit_rate(self) -> float:
        total = self.cache_hit + self.cache_miss
        return (self.cache_hit / total) if total else 0.0


metrics = MetricsRegistry()


def log_llm_call(
    call_type: str,
    model_name: str,
    cost_per_1k_input: float,
    cost_per_1k_output: float,
) -> Callable:
    def decorator(func: Callable):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            result = await func(*args, **kwargs)
            elapsed_ms = (time.perf_counter() - t0) * 1000

            usage: TokenUsage | None = getattr(result, "token_usage", None)
            if usage:
                estimated_cost = (
                    (usage.input_tokens / 1000) * cost_per_1k_input
                    + (usage.output_tokens / 1000) * cost_per_1k_output
                )
                logger.info(
                    "llm_call type=%s model=%s in=%d out=%d total=%d cost_usd=%.6f cached=%s latency_ms=%.1f",
                    call_type,
                    model_name,
                    usage.input_tokens,
                    usage.output_tokens,
                    usage.total_tokens,
                    estimated_cost,
                    usage.cached,
                    elapsed_ms,
                )
            else:
                logger.info(
                    "llm_call type=%s model=%s latency_ms=%.1f",
                    call_type,
                    model_name,
                    elapsed_ms,
                )
            return result

        return wrapper

    return decorator
