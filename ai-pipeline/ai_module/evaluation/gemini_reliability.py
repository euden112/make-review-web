from ai_module.evaluation.reduce_reliability import ReduceReliabilityResult, compute_reduce_reliability

# Gemini → Groq 모델 전환에 따른 이름 변경. 하위 호환 별칭 유지.
GeminiReliabilityResult = ReduceReliabilityResult
compute_gemini_reliability = compute_reduce_reliability

__all__ = [
    "GeminiReliabilityResult",
    "compute_gemini_reliability",
]
