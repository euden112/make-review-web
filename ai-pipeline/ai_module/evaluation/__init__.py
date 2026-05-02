from ai_module.evaluation.reduce_reliability import ReduceReliabilityResult, compute_reduce_reliability
from ai_module.evaluation.semantic_similarity import compute_semantic_similarity

# 하위 호환 별칭
GeminiReliabilityResult = ReduceReliabilityResult
compute_gemini_reliability = compute_reduce_reliability

__all__ = [
    "ReduceReliabilityResult",
    "compute_reduce_reliability",
    "GeminiReliabilityResult",
    "compute_gemini_reliability",
    "compute_semantic_similarity",
]
