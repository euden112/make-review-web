# 스프린트 기획 — AI Pipeline

> 대상 파일: `ai-pipeline/ai_module/`

---

## 변경 사항 요약

| 파일 | 변경 항목 |
|------|---------|
| `map_reduce/map_local.py` | MapResult에 토큰 필드 추가, 청크에 리뷰 메타 포함 |
| `map_reduce/reduce_api.py` | 토큰 필드 추가, 지역별 프롬프트 추가, 앵커링 데이터 수신 |
| `evaluation/gemini_reliability.py` | 신규 모듈 — Gemini 출력 신뢰도 4개 지표 |
| `evaluation/semantic_similarity.py` | 신규 모듈 — 임베딩 기반 요약 품질 평가 |

---

## map_local.py

### 1. MapResult에 토큰 필드 추가 (항목 05)

Ollama `/api/chat` 응답에 포함된 토큰 정보를 추출하여 저장한다.

```python
# 현재
@dataclass(slots=True)
class MapResult:
    chunk_no: int
    summary: str
    cached: bool

# 변경
@dataclass(slots=True)
class MapResult:
    chunk_no: int
    summary: str
    cached: bool
    input_tokens: int = 0   # data["prompt_eval_count"]
    output_tokens: int = 0  # data["eval_count"]
```

Ollama 응답에서 추출:

```python
summary = str(data.get("message", {}).get("content", "")).strip()
input_tokens  = data.get("prompt_eval_count", 0) or 0
output_tokens = data.get("eval_count", 0) or 0
return MapResult(chunk_no=chunk.chunk_no, summary=summary, cached=False,
                 input_tokens=input_tokens, output_tokens=output_tokens)
```

캐시 히트 시 토큰 소비 없음 → `input_tokens=0, output_tokens=0` 유지.

### 2. 청크 텍스트에 리뷰 메타 포함 (항목 06)

Gemini가 `representative_reviews`를 선택할 때 `helpful_count`와 `playtime_hours`를 참고할 수 있도록 청크 텍스트에 메타를 포함한다.

```python
# 현재 프롬프트
prompt = (
    "Summarize the following game review chunk in <= 6 sentences. "
    "Include pros, cons, technical issues(optimization, bugs), and evidence review_id.\n\n"
    f"{chunk.text}"
)

# 변경 — chunk.text 내 리뷰 형식에 메타 추가
# chunker.py 또는 sampler.py에서 청크 텍스트 구성 시:
# [review_id=42 helpful=15 playtime=38h] 리뷰 본문...
```

---

## reduce_api.py

### 1. FinalSummary에 토큰 필드 추가 (항목 05)

```python
@dataclass(slots=True)
class FinalSummary:
    ...
    reduce_input_tokens: int = 0   # response.usage_metadata.prompt_token_count
    reduce_output_tokens: int = 0  # response.usage_metadata.candidates_token_count
```

Gemini 응답에서 추출:

```python
return FinalSummary(
    ...
    reduce_input_tokens=response.usage_metadata.prompt_token_count or 0,
    reduce_output_tokens=response.usage_metadata.candidates_token_count or 0,
)
```

### 2. run_reduce_stage 시그니처 확장 (항목 06)

앵커링 데이터를 수신하여 프롬프트에 포함한다.

```python
async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    map_summaries: list[str],
    max_items: int = 24,
    timeout_sec: int = 180,
    # 추가
    score_anchors: dict | None = None,       # steam_recommend_ratio 등
    category_frequency: list[tuple] | None = None,  # [("버그/안정성", 38), ...]
) -> FinalSummary:
```

프롬프트 구성:

```python
anchor_block = ""
if score_anchors:
    anchor_block += "[score_anchors]\n"
    if score_anchors.get("steam_recommend_ratio") is not None:
        anchor_block += f"steam_recommend_ratio: {score_anchors['steam_recommend_ratio']:.1f}%\n"
    if score_anchors.get("metacritic_critic_avg") is not None:
        anchor_block += f"metacritic_critic_avg: {score_anchors['metacritic_critic_avg']}\n"
    if score_anchors.get("metacritic_user_avg") is not None:
        anchor_block += f"metacritic_user_avg: {score_anchors['metacritic_user_avg']}\n"
    anchor_block += "→ sentiment_score는 위 수치를 참고하여 산출할 것\n\n"

freq_block = ""
if category_frequency:
    freq_block = "[category_frequency]\n"
    freq_block += "\n".join(f"{cat}: {cnt}회" for cat, cnt in category_frequency)
    freq_block += "\n→ keywords에 상위 빈도 카테고리를 반드시 포함할 것\n\n"

user_prompt = (
    f"language={language_code}\n"
    + anchor_block
    + freq_block
    + "representative_reviews 선택 기준:\n"
    + "1. helpful_count 높은 리뷰 우선\n"
    + "2. playtime_hours 10시간 이상 리뷰 우선\n"
    + "3. 긍정/부정 균형 (각 1~2개)\n"
    + "4. 직접 인용 가능한 길이 (50-200자)\n\n"
    + "Integrate map summaries into a final sentiment-aware game review summary.\n\n"
    + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
)
```

### 3. 지역별 간략 프롬프트 추가 (항목 03)

```python
REGIONAL_REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only with keys: one_liner, full_text.
No markdown, no code fences.
""".strip()

REGIONAL_REDUCE_USER_TEMPLATE = """
language=ko
Briefly summarize how {region} players perceive this game in 2-3 sentences.
Focus on what makes their perspective distinctive compared to the general consensus.
Output in Korean.

{map_summaries}
"""
```

---

## evaluation/gemini_reliability.py (신규, 항목 07)

Gemini 출력의 신뢰도를 결정론적으로 검증한다. 추가 LLM 호출 없음.

```python
from dataclasses import dataclass
from ai_module.map_reduce.reduce_api import FinalSummary

@dataclass
class GeminiReliabilityResult:
    schema_compliance: float       # 0.0 ~ 1.0
    hallucination_score: float | None
    sentiment_consistency: int     # 0 or 1
    anchor_deviation: float | None


def compute_gemini_reliability(
    ai_result: FinalSummary,
    input_reviews: list,
    steam_recommend_ratio: float | None,
) -> GeminiReliabilityResult:

    # schema_compliance: 필수 9개 항목 검사
    checks = [
        bool(ai_result.one_liner),
        ai_result.sentiment_overall in {"positive", "mixed", "negative"},
        ai_result.sentiment_score is not None and 0 <= ai_result.sentiment_score <= 100,
        bool(ai_result.aspect_scores),
        bool(ai_result.pros),
        bool(ai_result.cons),
        bool(ai_result.keywords),
        bool(ai_result.representative_reviews),
        bool(ai_result.full_text),
    ]
    schema_compliance = sum(checks) / len(checks)

    # hallucination_score: representative_reviews review_id 실존 검사
    input_ids = {getattr(r, "id", None) for r in input_reviews}
    cited_ids = [
        r.get("review_id") for r in ai_result.representative_reviews
        if isinstance(r, dict) and r.get("review_id") is not None
    ]
    hallucination_score = (
        sum(1 for rid in cited_ids if rid in input_ids) / len(cited_ids)
        if cited_ids else None
    )

    # sentiment_consistency: 레이블 vs 수치 일치
    score = ai_result.sentiment_score
    label = ai_result.sentiment_overall
    if score is not None and label is not None:
        consistent = (
            (label == "positive" and score >= 65) or
            (label == "mixed"    and 35 <= score < 65) or
            (label == "negative" and score < 35)
        )
        sentiment_consistency = 1 if consistent else 0
    else:
        sentiment_consistency = None

    # anchor_deviation: AI 점수 vs Steam 추천율 편차
    anchor_deviation = (
        abs(score - steam_recommend_ratio) / 100
        if score is not None and steam_recommend_ratio is not None
        else None
    )

    return GeminiReliabilityResult(
        schema_compliance=schema_compliance,
        hallucination_score=hallucination_score,
        sentiment_consistency=sentiment_consistency,
        anchor_deviation=anchor_deviation,
    )
```

### 운영 임계 기준 (초기 실측 후 조정)

| 지표 | 경고 기준 | 의미 |
|------|---------|------|
| `schema_compliance` | < 0.8 | 필수 필드 누락 — 파이프라인 재실행 검토 |
| `hallucination_score` | < 1.0 | 존재하지 않는 리뷰 ID 인용 — 대표 리뷰 신뢰 불가 |
| `sentiment_consistency` | = 0 | 레이블/수치 불일치 — Gemini 판단 불안정 |
| `anchor_deviation` | > 0.3 | Steam 추천율과 30% 이상 이탈 — 수동 검토 |

---

## evaluation/semantic_similarity.py (신규, 항목 04)

리뷰(영어)와 요약(한국어)을 다국어 임베딩 공간에서 비교한다.

```python
from sentence_transformers import SentenceTransformer, util

_model = None

def _get_model() -> SentenceTransformer:
    global _model
    if _model is None:
        _model = SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")
    return _model


def compute_semantic_similarity(
    review_texts: list[str],  # quality_score 상위 N개 리뷰
    summary_text: str,
) -> float:
    model = _get_model()
    review_embs = model.encode(review_texts, convert_to_tensor=True)
    summary_emb = model.encode(summary_text, convert_to_tensor=True)
    avg_review_emb = review_embs.mean(dim=0)
    score = util.cos_sim(avg_review_emb, summary_emb).item()
    return round(float(score), 4)
```

> **주의**: 동기 CPU 연산. `ai_service.py`에서 호출 시 반드시 `loop.run_in_executor(None, compute_semantic_similarity, ...)` 로 감싸야 FastAPI 이벤트 루프 블로킹을 방지한다.
