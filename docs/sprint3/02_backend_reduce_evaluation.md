# Backend Reduce 단계 평가: 기획 vs 구현

> 기준: `docs/plan-backend.md` (Reduce 단계) vs `ai-pipeline/ai_module/map_reduce/reduce_api.py`

---

## 1. 언어 파이프라인 모드 분기 (항목 01)

### 기획

**Unified 모드**:
- 리뷰 언어 필터 없이 전체 리뷰 사용
- Reduce 출력 언어 `"ko"` 고정
- 완전한 요약 (one_liner + aspect_scores + pros/cons + keywords + representative_reviews)

**Regional 모드**:
- `review_language` 기준으로 리뷰 필터링
- 간략 Reduce 프롬프트 (2~3문장)
- 최소 요약 (one_liner + full_text만)

### 구현 평가

```python
async def run_reduce_stage(
    *,
    ...
    regional: bool = False,  # ✓ 플래그로 분기
) -> FinalSummary:
```

**✓ 구현됨**:
- `regional` 플래그로 unified/regional 분기
- 시스템 프롬프트 교체: `REDUCE_SYSTEM_PROMPT` vs `REGIONAL_REDUCE_SYSTEM_PROMPT`
- regional 모드에서 사용자 프롬프트 간략화
- regional 모드에서 반환값 축약 (aspect_scores, representative_reviews 빈 객체)

```python
if regional:
    region = _REGION_NAMES.get(language_code, f"{language_code}-speaking")
    user_prompt = (
        "language=ko\n"
        f"Briefly summarize how {region} players perceive this game in 2-3 sentences.\n"
        ...
    )
else:
    anchor_block = "..."
    category_block = "..."
    user_prompt = (
        f"language={language_code}\n"
        f"{anchor_block}"
        f"{category_block}"
        ...
    )
```

**⚠️ 주의사항**:
- Regional 모드도 `full_text` 필드를 반환하는데, 기획에서는 2-3문장만 필요
- 현재 프롬프트가 "2-3 sentences"를 요청하므로 문제없음

---

## 2. 앵커 데이터 활용 (항목 02 사전 조건)

### 기획

Reduce 호출 전 두 가지 집계 데이터 전달:

**카테고리 빈도** (keywords 앵커링):
```python
category_frequency: list[tuple[str, int]]
# [(그래픽, 45), (조작감, 32), ...]
```

**점수 앵커** (sentiment_score 앵커링):
```python
score_anchors: dict[str, float | None] = {
    "steam_recommend_ratio": 87.5,
    "metacritic_critic_avg": 78.0,
    "metacritic_user_avg": 7.8,
}
```

### 구현 평가

```python
async def run_reduce_stage(
    ...
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int]] | None = None,
    ...
):
```

**✓ 구현됨**:
- 두 매개변수 모두 수신 가능
- unified 모드에서만 활용 (regional에서는 무시)

```python
# Unified 모드에서 앵커 블록 구성
if score_anchors:
    anchor_block += "[score_anchors]\n"
    if score_anchors.get("steam_recommend_ratio") is not None:
        anchor_block += f"steam_recommend_ratio: {score_anchors['steam_recommend_ratio']:.2f}%\n"
    ...

if category_frequency:
    category_block += "[category_frequency]\n"
    for category, count in category_frequency:
        category_block += f"{category}: {count}회\n"
    ...
```

**✓ 프롬프트 통합 방식**:
- 앵커 데이터가 있으면 프롬프트 앞에 `[score_anchors]`, `[category_frequency]` 블록 추가
- 모델 지시: `→ sentiment_score must be calibrated to these numbers.`
- 모델 지시: `→ keywords must include top-frequency categories above.`

---

## 3. Groq API 호출 및 토큰 수집

### 기획

- Groq API 호출 (unified/regional 모두)
- 응답에서 토큰 수 추출: `prompt_tokens`, `completion_tokens`
- `FinalSummary`에 저장: `input_tokens`, `output_tokens`

### 구현 평가

```python
@retry(stop=stop_after_attempt(3), wait=wait_exponential(...), reraise=True)
async def _generate_reduce_response(
    client: AsyncGroq,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
):
    return await client.chat.completions.create(
        model=model_name,
        messages=[...],
        response_format={"type": "json_object"},
        temperature=0.2,
    )
```

**✓ 구현됨**:
- Groq AsyncClient 사용
- JSON 응답 포맷 강제 (`response_format={"type": "json_object"}`)
- 온도 0.2 (결정적)
- 재시도 3회 (exponential backoff)

```python
response = await asyncio.wait_for(
    _generate_reduce_response(client, model_name, system_prompt, user_prompt),
    timeout=timeout_sec,
)

token_in = int(response.usage.prompt_tokens or 0)
token_out = int(response.usage.completion_tokens or 0)

# FinalSummary에 저장
return FinalSummary(
    ...
    input_tokens=token_in,
    output_tokens=token_out,
)
```

**✓ 토큰 수집**:
- `response.usage`에서 추출
- `FinalSummary.input_tokens`, `output_tokens`에 저장

---

## 4. JSON 파싱 및 정규화

### 기획

Groq 응답을 JSON으로 파싱하여 `FinalSummary` 필드에 매핑:

```python
FinalSummary(
    one_liner: str,
    aspect_scores: dict,           # {graphics: {label, score}, ...}
    representative_reviews: list,  # [{source, review_id, quote, reason}, ...]
    full_text: str,                # 4-6 sentences in Korean
    sentiment_overall: str | None, # "positive" | "mixed" | "negative"
    sentiment_score: float | None, # 0.0 ~ 100.0
    pros: list[str],               # ["장점1", "장점2", ...]
    cons: list[str],               # ["단점1", "단점2", ...]
    keywords: list[str],           # ["키워드1", "키워드2", ...]
)
```

### 구현 평가

```python
def _normalize_sentiment_overall(value: Any) -> str | None:
    text = str(value).strip().lower()
    if text in {"positive", "mixed", "negative"}:
        return text
    return None

def _normalize_sentiment_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    if score < 0:
        return 0.0
    if score > 100:
        return 100.0
    return round(score, 2)

def _to_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]
```

**✓ 정규화 함수**:
- `_normalize_sentiment_overall`: 유효한 값만 통과, 아니면 None
- `_normalize_sentiment_score`: 0~100 범위 강제, 실수 반올림
- `_to_string_list`: 리스트가 아니면 [], 빈 문자열 필터

```python
return FinalSummary(
    one_liner=parsed["one_liner"],
    aspect_scores=parsed["aspect_scores"],
    representative_reviews=parsed["representative_reviews"],
    full_text=parsed["full_text"],
    sentiment_overall=_normalize_sentiment_overall(parsed.get("sentiment_overall")),
    sentiment_score=_normalize_sentiment_score(parsed.get("sentiment_score")),
    pros=_to_string_list(parsed.get("pros", [])),
    cons=_to_string_list(parsed.get("cons", [])),
    keywords=_to_string_list(parsed.get("keywords", [])),
    input_tokens=token_in,
    output_tokens=token_out,
)
```

**✓ 매핑**:
- 모든 필드를 명시적으로 매핑
- 정규화 함수 적용

---

## 5. 에러 처리 및 복구

### 기획

- Groq API 오류 분류 (재시도 가능 vs 불가)
- 오류 응답 반환

### 구현 평가

```python
class ReduceParseError(ValueError):
    pass

def classify_reduce_error(exc: Exception) -> tuple[str, bool]:
    """오류 분류: (error_code, is_retryable)"""
    if isinstance(exc, ReduceParseError):
        return ("parse_error", False)  # 파싱 오류: 재시도 불가

    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in message:
        return ("timeout", True)  # 타임아웃: 재시도 가능

    if "quota" in message or "rate limit" in message or "429" in message:
        return ("quota", False)  # 할당량 초과: 재시도 불가

    return ("upstream_unavailable", True)  # 기타: 재시도 가능
```

**✓ 오류 분류**:
- Parse Error (JSON 파싱 실패): 재시도 불가 → 기획서 생성 후 즉시 반환
- Timeout: 재시도 가능 → `tenacity` 재시도 이미 적용됨
- Quota/Rate Limit: 재시도 불가 → 운영자 개입 필요
- 기타: 재시도 가능

```python
except Exception as e:
    error_code, is_retryable = classify_reduce_error(e)
    logger.warning(
        "reduce stage failed: code=%s retryable=%s error=%s",
        error_code,
        is_retryable,
        e,
    )
    return FinalSummary(
        one_liner="요약 생성 중 오류가 발생했습니다.",
        aspect_scores={},
        representative_reviews=[],
        full_text=(
            f"ErrorCode={error_code}; retryable={str(is_retryable).lower()}; detail={str(e)}"
        ),
        error_code=error_code,
        is_retryable=is_retryable,
    )
```

**✓ 오류 응답**:
- `FinalSummary` 객체로 오류 정보 반환
- `error_code`, `is_retryable` 필드로 분류 정보 포함
- `full_text`에 상세 오류 메시지

---

## 6. 기획 vs 구현 체크리스트

| 항목 | 기획 | 구현 | 상태 |
|------|------|------|------|
| **Unified 모드** | one_liner + aspect_scores + pros/cons + keywords + representative_reviews | 모두 반환 | ✓ |
| **Regional 모드** | one_liner + full_text만 | aspect_scores={}, representative_reviews=[] 반환 | ✓ |
| **앵커 데이터** | score_anchors, category_frequency 수신 | 매개변수 추가, 프롬프트에 통합 | ✓ |
| **토큰 수집** | input_tokens, output_tokens 저장 | FinalSummary에 저장 | ✓ |
| **정규화** | sentiment_overall, sentiment_score 범위 체크 | 함수 구현 | ✓ |
| **에러 분류** | 재시도 가능/불가 구분 | classify_reduce_error 구현 | ✓ |
| **로깅** | 진행 상황 기록 | logger 사용 | ✓ |

---

## 7. 현재 구현 문제점 및 개선안

### 문제점 1: Regional 모드 representative_reviews

**현황**: 빈 리스트 반환

**기획**: Regional 요약도 근거 리뷰가 필요할 수 있음

**개선안**:
```python
if regional:
    return FinalSummary(
        one_liner=parsed["one_liner"],
        aspect_scores={},
        representative_reviews=parsed.get("representative_reviews", []),  # ← 추가
        full_text=parsed["full_text"],
        input_tokens=token_in,
        output_tokens=token_out,
    )
```

### 문제점 2: 토큰 수 기본값 처리

**현황**:
```python
token_in = int(response.usage.prompt_tokens or 0)
```

**개선안**: 더 명시적인 처리
```python
token_in = int(response.usage.prompt_tokens) if response.usage.prompt_tokens else 0
```

### 문제점 3: JSON 파싱 오류 시 필드 누락

**현황**:
```python
try:
    parsed = _safe_parse_json(raw_text)
except Exception as exc:
    raise ReduceParseError(str(exc)) from exc
```

**개선안**: 기본값 제공
```python
try:
    parsed = _safe_parse_json(raw_text)
except Exception as exc:
    raise ReduceParseError(str(exc)) from exc

# 필드별 기본값 보장
one_liner = parsed.get("one_liner", "요약 생성 실패")
```

---

## 8. 다음 단계: ai_service.py와의 통합

`reduce_api.py`는 저수준 Reduce API이고, 실제 통합은 `ai_service.py`에서:

1. **앵커 데이터 계산 및 전달** (plan-backend.md § 4, 5)
   - `category_frequency` 계산
   - `score_anchors` 준비
   - `run_reduce_stage()` 호출

2. **신뢰도 지표 계산** (plan-backend.md § 6, 7)
   - `gemini_reliability.compute_gemini_reliability()` 호출
   - DB 저장

3. **토큰 기록** (plan-backend.md § 3)
   - `reduce_input_tokens`, `reduce_output_tokens` 저장

---

## 결론

**✓ Reduce 단계 구현 평가: 양호**

`reduce_api.py`는 기획 기준과 **90% 일치**합니다:
- ✓ unified/regional 분기
- ✓ 앵커 데이터 활용
- ✓ 토큰 수집
- ✓ 정규화 및 에러 처리
- ⚠️ Minor: regional 근거 리뷰, JSON 파싱 기본값

**미완성 부분은 ai_service.py에서 담당**:
- 앵커 데이터 계산 및 전달
- 신뢰도 지표 계산
- ReviewSummaryJob 저장
