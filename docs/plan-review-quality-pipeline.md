# 리뷰 품질 개선 파이프라인 기획

## 1. 목표

현재 AI 요약 파이프라인은 Map 단계에서 텍스트 블록을 구조 비슷한 문자열로 만들고, Reduce 단계에서 단일 프롬프트로 `unified`, `playtime`, `critic`, `user` 결과를 한 번에 생성한다. 이 방식은 구현은 단순하지만 다음 문제가 있다.

- Map 출력이 JSON이 아니라 문자열 헤더 기반이라 검증과 재처리가 약하다.
- Reduce 프롬프트가 너무 많은 기능을 한 번에 요구해 섹션 간 문장 반복, 근거 혼합, 누락이 발생하기 쉽다.
- 토큰 압박 때문에 중요한 기능별 근거가 한 프롬프트 안에서 서로 경쟁한다.
- 실패 시 어느 기능이 실패했는지 분리하기 어렵고, 전체 재시도가 필요하다.

목표는 Map 출력은 JSON으로 구조화하고, Reduce는 단일 프롬프트가 아니라 기능별 파이프라인으로 나누어 요약 품질을 높이는 것이다. 단, 하루 50개 게임 처리 목표와 외부 LLM 한도를 넘지 않아야 한다.

여기서 말하는 품질 향상은 "Metacritic 유저들은 전투를 칭찬했다"처럼 범주만 말하는 일반론이 아니다. 실제 유저가 남긴 리뷰의 구체 표현과 경험 조건을 요약에 남기는 것이 목표다. 다만 구체성을 확보한다는 이유로 특정 보스명, 후반 지역명, 엔딩명, 반전, 캐릭터 사망, 퀘스트 결말처럼 미경험자에게 스포일러가 될 수 있는 정보를 공개 출력에 그대로 노출해서는 안 된다.

예를 들어 "전투가 좋다"는 너무 일반적이지만, "불의 거인 보스전에서 반복 사망했다"는 스포일러 위험이 있다. 공개 요약에서는 "후반부 대형 보스전에서 반복 실패가 누적되며 피로감을 느꼈다는 반응이 있다"처럼 원문 리뷰의 경험을 보존하되 고유명사와 사건 결말을 추상화해야 한다.

따라서 새 파이프라인의 품질 기준은 다음이다.

- 플랫폼/집단 단위의 두루뭉실한 문장보다 실제 review id에 연결된 구체 근거를 우선한다.
- pros/cons/keywords/aspect score는 가능한 한 원문 snippet 또는 Map JSON의 `evidence_items`와 연결한다.
- LLM이 "있을 법한 장점"을 꾸며내지 못하도록 score anchor는 톤 보정에만 쓰고, 구체 문장 근거는 원문 리뷰에서 가져온다.
- 최종 문장은 짧은 인용 복사가 아니라 여러 원문 근거를 압축해 자연어로 재서술한다.
- 공개 출력은 스포일러 안전성을 만족해야 한다. 특정 보스명, 엔딩명, 후반 지역명, 반전, 캐릭터 사망, 퀘스트 결말은 내부 evidence에는 보존할 수 있지만 `summary`, `pros`, `cons`, `keywords`, `one_liner`, `recommended_for`, `caution_for`, `evaluation_criteria`에는 그대로 노출하지 않는다.

이 품질 기준은 특정 요약에만 적용하지 않는다. `user_summaries`, `critic_summaries`, `playtime_analyses`, `game_review_summaries`에 저장되는 모든 자연어 출력이 같은 기준을 만족해야 한다. `summary` 본문뿐 아니라 `pros`, `cons`, `keywords`, `aspect_scores.label`, `one_liner`, `recommended_for`, `caution_for`, `evaluation_criteria`도 실제 리뷰 근거 없이 일반론으로 채우면 실패로 본다.

### 1-1. 스포일러 안전 구체성 기준

새 기준은 **내부 근거 보존**과 **공개 출력 안전화**를 분리한다.

| 계층 | 허용 | 금지/주의 |
|---|---|---|
| Map internal evidence | 원문 snippet, review_id, 구체 명칭 보존 가능 | LLM이 원문에 없는 명칭을 새로 생성하면 실패 |
| Reduce 입력 | 가능하면 `public_detail` 또는 redacted detail 우선 사용 | raw spoiler detail을 그대로 최종 문장에 복사하지 않음 |
| 공개 출력 | 경험 유형, 플레이 조건, 불만/만족 이유, 영향 설명 | 특정 보스명, 엔딩명, 반전, 캐릭터 사망, 후반 지역명, 퀘스트 결말 |

표현 기준:

| 나쁜 표현 | 이유 | 좋은 표현 |
|---|---|---|
| "불의 거인 보스전에서 반복 사망했다." | 특정 보스명 노출 | "후반부 대형 보스전에서 반복 실패로 피로감을 느꼈다는 반응이 있다." |
| "미친불 엔딩 컷신 후 강제종료가 발생했다." | 특정 엔딩명 노출 | "특정 엔딩 연출 이후 강제 종료를 겪었다는 보고가 있다." |
| "레아 루카리아 NPC/보스 버그가 있다." | 지역명과 진행 요소 노출 | "특정 중반 지역의 NPC/보스 진행 관련 버그 경험이 보고된다." |
| "보스전이 어렵다." | 너무 일반적 | "패턴 학습을 요구하는 고난도 전투에서 반복 실패와 성취감이 함께 언급된다." |

## 2. 현재 구조 요약

현재 흐름:

1. `stratified_select_reviews()`가 리뷰를 선별한다.
2. `chunk_reviews_by_chars()`가 청크를 만든다.
3. `run_map_stage()`가 로컬 Ollama로 청크별 문자열 요약을 만든다.
4. `run_reduce_stage()`가 Groq API 한 번으로 최종 JSON 전체를 생성한다.
5. `ai_service.py`가 결과를 `game_review_summaries`, `playtime_analyses`, `critic_summaries`, `user_summaries`에 저장한다.

현재 계측:

- `ReviewSummaryJob.map_input_tokens`
- `ReviewSummaryJob.map_output_tokens`
- `ReviewSummaryJob.reduce_input_tokens`
- `ReviewSummaryJob.reduce_output_tokens`

일일 요청/토큰 한도 추적 대상은 외부 API를 호출하는 Reduce 단계로 한정한다. `map_input_tokens`, `map_output_tokens`는 로컬 Ollama의 성능/품질 디버깅 참고값으로만 유지하고, 500K TPD/30K TPM/1K RPD 계산에는 포함하지 않는다.

기능별 Reduce로 분리되면 `reduce_*_tokens`는 외부 API 사용량 총합으로 저장하고, 상세 내역은 `failure_reasons_json.reduce_usage` 또는 신규 `reduce_usage_json` 필드로 확장하는 것이 좋다.

## 2-1. 현재 main에서 반드시 보존할 품질 장치

새 파이프라인은 현재 main에 이미 병합된 품질 보강을 대체하는 것이 아니라, 그 위에 Map/Reduce 구조를 개선하는 방식이어야 한다. 다음 장치는 새 구조에서도 필수 전제로 유지한다.

| 현재 장치 | 현재 역할 | 새 파이프라인에서의 유지 방식 |
|---|---|---|
| 언어 필터 | Steam 리뷰는 `english`, `koreana` 중심으로 통과시키고, 필터 후 리뷰가 너무 적으면 원본으로 fallback | Map 입력 전 필터 단계로 유지. JSON Map은 필터링된 리뷰만 처리하되 fallback 여부를 job metadata에 기록 |
| 스팸 리뷰 제거 | `is_spam_review()`로 노이즈 리뷰 제거 | 선별 전 유지 |
| 동적 플랫폼 비율 | Steam/Metacritic 실제 유효 리뷰 수에 따라 예산을 배분 | 기능별 Reduce 전에도 동일하게 유지 |
| Steam 긍정/부정 균형 | 추천/비추천 비율을 반영해 유저 리뷰 편향 완화 | User Reduce 입력의 기본 샘플링 계약으로 유지 |
| Metacritic 점수 bin 균형 | low/mid/high 비평 리뷰를 나누어 선별 | Critic Reduce 입력의 기본 샘플링 계약으로 유지 |
| 플레이타임 버킷 균형 | early/mid/late를 p33/p66 기준으로 나누고 부족 bucket은 `_ensure_bucket_coverage()`로 보강 | Playtime Reduce 입력의 필수 전처리로 유지 |
| 선별 수 부족 시 fallback | 목표 수보다 적게 선별되면 `quality_score()` 순으로 추가 충원 | 기능별 입력 부족을 줄이기 위해 유지 |
| 점수 anchor | Steam 추천률, Metacritic critic/user 평균을 Reduce에 제공 | 기능별 Reduce와 final composer 모두에서 hallucination 방지 anchor로 유지 |
| 카테고리 통계 | `category_frequency`로 pros/cons/keywords와 aspect score의 근거 제공 | User Reduce와 final composer 입력에 유지 |
| 대표 인용 | 원문 리뷰에서 aspect 키워드 밀집 구간을 deterministic하게 추출 | LLM이 만든 quote로 대체하지 않고, 원문 기반 quote를 유지 |
| 저장 구조 | `summary_text=None`, 본문은 `user_summaries`, `critic_summaries`, `playtime_analyses`에서 제공 | 새 final composer도 이 저장 정책을 유지 |

따라서 이 기획의 핵심은 "현재 main의 선별/근거/저장 정책을 유지한 채 Map 출력과 Reduce 호출 구조만 더 검증 가능하게 바꾸는 것"이다.

## 3. 외부 한도와 50게임/일 예산

주어진 한도:

| 항목 | 한도 |
|---|---:|
| Requests per Minute | 30 |
| Requests per Day | 1,000 |
| Tokens per Minute | 30,000 |
| Tokens per Day | 500,000 |

50게임/일 기준 평균 예산:

| 항목 | 게임당 평균 예산 |
|---|---:|
| 요청 수 | 20 req/game |
| 토큰 수 | 10,000 tokens/game |

Map 단계는 현재 로컬 Ollama이므로 외부 요청/토큰 한도에는 포함하지 않는다. 외부 한도와 일일 사용량 추적은 Groq 기반 Reduce 계열 호출에만 적용한다. 만약 Map도 외부 모델로 전환하면 그때부터 별도 외부 API 예산으로 분리 산정해야 하며, 현재 Reduce 한도 계산에 섞지 않는다.

계산식:

- `requests/day = games/day * reduce_requests_per_game * (1 + retry_rate)`
- `tokens/day = games/day * reduce_tokens_per_game * (1 + retry_rate)`
- `tokens/minute ~= concurrent_games * reduce_tokens_per_game_per_minute`

이 문서의 기본 가정은 `games/day=50`, `reduce_requests_per_game=4`, `reduce_tokens_per_game=9,800`이다. 단, `9,800 tokens/game`은 현재 코드의 실제 계측값이 아니라 한도 내 품질 최대화 목표 예산이다. 기능별 Reduce 구현 후 5게임 dry-run에서 `ReviewSummaryJob`의 사용량과 provider usage를 기준으로 반드시 갱신한다.

## 4. 권장 일일 파이프라인 예산

일일 기본 파이프라인은 게임당 4회 외부 Reduce 호출을 기준으로 한다.

| 단계 | 목적 | 입력 토큰 추정 | 출력 토큰 추정 | 합계 |
|---|---|---:|---:|---:|
| Reduce A | 유저 리뷰 요약 | 2,700 | 1,100 | 3,800 |
| Reduce B | 비평가 리뷰 요약 | 900 | 550 | 1,450 |
| Reduce C | 플레이타임 버킷 요약 | 1,700 | 900 | 2,600 |
| Reduce D | 최종 조립: 한줄평, aspect score, pros/cons, keywords | 1,300 | 650 | 1,950 |
| 합계 | 4 requests/game | 6,600 | 3,200 | 9,800 |

50게임/일 사용량:

| 항목 | 사용량 | 한도 대비 |
|---|---:|---:|
| 요청 수 | 200 req/day | 20% |
| 토큰 수 | 490,000 tokens/day | 98% |

재시도 예산:

- 9.8K/game 품질 최대화 모드에서는 50게임 전체 배치에 대한 즉시 retry를 허용하지 않는다.
- 1% 재시도만 발생해도 약 `494,900 tokens/day`, `202 req/day`로 한도에 매우 근접한다.
- 2% 재시도는 약 `499,800 tokens/day`, `204 req/day`로 사실상 일일 한도 상한이다.
- 2% 초과 재시도는 500K 토큰 한도를 넘을 수 있으므로 다음 날 지연 큐로 넘긴다.

따라서 한도 내 길이를 최대화하는 운영 정책은 `기능별 Reduce 4회 + 50게임 전체 490K/day 목표 + 즉시 retry 금지`로 잡는다.

챗봇이 같은 Groq 프로젝트 한도를 공유한다면 9.8K/game 모드는 그대로 사용할 수 없다. 이 모드는 배치 전용 한도를 거의 전부 쓰는 설정이다. 챗봇/수동 재생성/retry와 한도를 공유해야 하는 날에는 안정 운영 모드인 8.6K/game, 430K/day로 낮춘다.

## 5. 대안별 트레이드오프

### A안: 단일 Reduce 유지

| 항목 | 값 |
|---|---:|
| 요청 수 | 1 req/game, 50 req/day |
| 토큰 수 | 약 6,000~8,000 tokens/game |
| 장점 | 저렴하고 단순함 |
| 단점 | 품질 병목이 큼. 기능별 실패 분리가 어렵고 섹션 간 중복/혼합이 잦음 |

품질 개선 목표와 맞지 않으므로 유지하지 않는다.

### B안: 품질 최대화, 기능별 4 Reduce

| 항목 | 값 |
|---|---:|
| 요청 수 | 4 req/game, 200 req/day |
| 토큰 수 | 약 9,800 tokens/game, 490K/day |
| 장점 | 4회 Reduce 구조를 유지하면서 본문 길이를 한도 가까이 확장 |
| 단점 | retry, 챗봇, 수동 재생성 여유가 거의 없음 |

리뷰 품질을 최우선으로 하는 50게임 배치에 채택한다.

### B-2안: 안정 운영, 기능별 4 Reduce

| 항목 | 값 |
|---|---:|
| 요청 수 | 4 req/game, 200 req/day |
| 토큰 수 | 약 8,600 tokens/game, 430K/day |
| 장점 | 10% 수준의 retry 또는 챗봇 트래픽 여유를 확보 |
| 단점 | User/Playtime 본문 길이가 B안보다 짧음 |

한도를 챗봇과 공유하거나 provider 응답이 불안정한 날의 fallback 정책으로 사용한다.

### C안: 고품질 5~6 Reduce

예: user, critic, playtime, aspect, representative evidence, final composer를 모두 분리.

| 항목 | 값 |
|---|---:|
| 요청 수 | 5~6 req/game, 250~300 req/day |
| 토큰 수 | 약 9,800~12,000 tokens/game |
| 장점 | 기능별 품질 최대화 |
| 단점 | 50게임 전체 일일 처리에는 토큰 한도 초과 가능성이 높음 |

전체 50게임 기본 배치에는 쓰지 않고, 상위 노출 게임이나 수동 재생성에만 사용한다.

## 6. 분당 처리량 정책

일일 총량보다 실제 병목은 `Tokens per Minute 30K`다.

권장안 B의 게임당 외부 토큰은 약 9.8K다. 이론상 한 분에 3게임을 처리하면 약 29.4K로 한도 안에 들어오지만, 출력 길이와 재시도 변동을 고려하면 안전하지 않다.

운영 정책:

- 기본 동시 처리: 2 games concurrently
- Reduce 호출은 token bucket으로 제어
- 분당 예약 토큰은 22K 이하로 제한
- 429 또는 quota 응답이 오면 해당 게임의 남은 feature reduce를 지연 큐로 이동

50게임 처리 예상:

- 2게임 동시 처리, 게임당 4회 순차 Reduce 기준
- LLM 응답 20~40초/호출이면 전체 배치는 약 35~70분
- 요청 수 한도는 여유가 크고, TPM/TPD가 실질 제한이다.

## 7. Map JSON 구조화 설계

현재 Map 출력은 다음 헤더 기반 문자열이다.

```text
PROS:
CONS:
ASPECTS:
IDS:
```

이를 JSON으로 바꾼다. Map 단계에서 로컬 LLM을 사용한다는 원래 기획은 유지한다. 단, 실제 dry-run 결과 `qwen2.5:1.5b`가 JSON evidence를 안정적으로 생성하지 못했으므로, Map 단계는 "로컬 LLM 응답을 그대로 신뢰한다"는 전제에 의존하면 안 된다. 기본 설계는 다음 순서를 따른다.

1. 원문 리뷰에서 deterministic evidence candidate를 먼저 생성한다.
2. 로컬 Ollama Map은 candidate와 원문 chunk를 함께 받아 `evidence_items`, `aspects`, `sentiment`, `quote_candidates`를 JSON으로 생성한다.
3. Ollama 결과가 JSON schema, review_id 검증, evidence 품질 검증을 통과하면 Ollama 결과를 1차 Map 산출물로 사용한다.
4. Ollama 결과가 부분적으로만 유효하면 deterministic candidate를 기준으로 누락 필드를 보강하고, `warnings`에 보강 사유를 기록한다.
5. Ollama 호출 실패, JSON parse 실패, `evidence_items` 공백, review_id 불일치가 발생하면 deterministic candidate를 fallback으로 사용한다.

즉, `evidence_items`의 목표 생성자는 로컬 Ollama Map이다. deterministic extractor는 로컬 LLM을 대체하는 기본 경로가 아니라, 원문 기반 후보 생성기이자 schema 검증 실패 시 복구 장치다.

```json
{
  "chunk_no": 1,
  "review_ids": [101, 102],
  "source_mix": {
    "steam_user": 12,
    "metacritic_user": 3,
    "metacritic_critic": 2
  },
  "sentiment": {
    "positive": 6,
    "mixed": 4,
    "negative": 7
  },
  "aspects": {
    "graphics": {
      "pros": ["dense world art direction"],
      "cons": ["occasional texture pop-in"],
      "evidence_ids": [101, 108]
    },
    "controls": {
      "pros": [],
      "cons": ["keyboard controls feel clunky"],
      "evidence_ids": [102]
    }
  },
  "playtime_signals": {
    "early": ["strong first impression"],
    "mid": ["difficulty spike"],
    "late": ["endgame repetition"]
  },
  "critic_signals": {
    "praise": ["ambitious level design"],
    "criticism": ["uneven pacing"],
    "evidence_ids": [201]
  },
  "quote_candidates": [
    {
      "review_id": 101,
      "polarity": "positive",
      "aspect": "content",
      "snippet": "..."
    }
  ],
  "evidence_items": [
    {
      "review_id": 101,
      "source": "steam_user",
      "aspect": "sound",
      "polarity": "positive",
      "detail": "late-game boss music heightens tension during repeated dodging and counterattacks",
      "public_detail": "후반부 고난도 전투의 음악과 패턴 회피가 긴장감을 높인다는 반응",
      "spoiler_risk": "medium",
      "snippet": "..."
    }
  ],
  "warnings": []
}
```

검증 규칙:

- JSON object만 허용한다.
- `review_ids`는 비어 있으면 실패 처리한다.
- `aspects.*.evidence_ids`는 실제 chunk review_ids 안에 있어야 한다.
- 허용 aspect는 `graphics`, `controls`, `optimization`, `content`, `price_value`, `sound`, `difficulty`, `multiplayer`, `bugs`로 제한한다.
- `evidence_items[].review_id`는 실제 chunk review_ids 안에 있어야 한다.
- `evidence_items[].detail`은 "combat is good" 같은 범주형 평가가 아니라 원문에서 확인 가능한 구체 단서여야 한다.
- `evidence_items[].snippet`은 원문에서 가져온 짧은 구간이어야 하며, LLM이 새로 지어낸 문장을 넣지 않는다.
- `evidence_items[].detail`과 `snippet`에는 내부 검증을 위해 원문 수준의 구체성이 남을 수 있다. 단, 공개 출력에 사용할 때는 `public_detail` 또는 redacted 표현을 우선한다.
- `public_detail`은 스포일러 고유명사를 제거하되 경험 조건, 문제 유형, 감정 영향은 유지해야 한다.
- `spoiler_risk`는 `none|low|medium|high` 중 하나로 기록한다. 특정 보스명, 엔딩명, 후반 지역명, 반전, 캐릭터 사망, 퀘스트 결말이 포함되면 최소 `medium`, 결말/반전/사망은 `high`로 본다.
- JSON parse 실패 시 Map 1회 재시도.
- 재시도 실패 시 해당 chunk는 drop하되 `failure_reasons_json.map_json_invalid`에 기록한다.

캐시 키는 로컬 LLM Map 기본 경로 전환 이후 `prompt_version="json_v2_llm_map"`로 분리한다. 기존 deterministic-primary 캐시와 섞이면 Map 성공률과 품질 측정이 왜곡되므로 같은 캐시 버전을 재사용하지 않는다.

Map 단계의 핵심 산출물은 `summary`가 아니라 `evidence_items`다. Reduce가 좋은 문장을 만들려면 "전투", "음악", "난이도" 같은 라벨만으로는 부족하다. 각 chunk에서 실제 리뷰가 말한 구체 상황, 감각, 불만 조건을 작은 evidence 단위로 보존해야 한다. 다만 공개 출력은 raw detail을 그대로 복사하지 않고 스포일러 안전 표현으로 압축해야 한다.

### 7-1. Dry-Run 반영 보완

2026-05-25 기준 실제 API/Ollama dry-run 결과:

| 게임 | 리뷰 제한 | 소요 | Map 결과 | Reduce 사용량 | 품질 판정 |
|---|---:|---:|---|---:|---|
| ELDEN RING | 36개 | 136.5초 | 2 chunks 모두 deterministic fallback | 15,221 input / 935 output | 미달 |
| Grand Theft Auto V | 36개 | 67.2초 | 1 chunk deterministic fallback | 9,150 input / 1,075 output | 미달 |

관찰:

- `qwen2.5:1.5b`는 JSON 대신 설명문을 출력하거나, `evidence_items`가 빈 JSON을 반환했다.
- fallback 후에도 Reduce 결과가 "긍정적인 평가", "콘텐츠와 멀티플레이를 높이 평가"처럼 일반론에 머물렀다.
- 36개 리뷰 제한에서도 Reduce input이 9K~15K로 커져 9.8K/game 예산을 초과할 수 있다.
- playtime bucket 근거가 부족한데도 playtime reduce가 호출되어 토큰을 소비했다.

따라서 보완 원칙은 다음이다.

- 로컬 Ollama Map을 기본 evidence 생성 경로로 유지한다.
- deterministic evidence extractor는 원문 기반 candidate와 fallback을 제공하되, 정상 경로에서는 Ollama Map 결과가 최종 Map JSON이 된다.
- Ollama Map prompt는 candidate를 단순 복사하지 않고 더 구체적인 `detail`, 더 정확한 `aspect/polarity`, 대표 `quote_candidates`를 생성하도록 설계한다.
- Ollama Map 결과가 candidate보다 품질이 낮거나 review_id/source가 불일치하면 validator가 candidate 기반 보강 또는 fallback을 수행한다.
- feature reduce 입력은 evidence를 30~50개로 압축하고, 동일 aspect/detail/snippet 중복을 제거한다.
- playtime bucket별 evidence가 부족하면 playtime reduce를 호출하지 않고 해당 bucket을 `null`로 둔다.
- 5게임 dry-run은 위 보완 후 다시 수행한다. 현재 결과만으로는 50게임/일 품질 최대화 운영안을 확정하지 않는다.

2026-05-26 보완 후 dry-run 결과:

| 게임 | 리뷰 제한 | chunks | Map 결과 | Reduce 사용량 | 품질 판정 |
|---|---:|---:|---|---:|---|
| ELDEN RING | 36개 | 4 | LLM repaired 4 / fallback 0 | 8,188 input / 1,262 output | 통과 |
| Grand Theft Auto V | 36개 | 3 | LLM repaired 3 / fallback 0 | 4,812 input / 1,507 output | 통과 |

확인된 개선:

- Map 단계는 deterministic primary가 아니라 로컬 Ollama 기본 경로로 동작한다.
- `qwen2.5:1.5b`가 schema를 벗어나거나 JSON을 일부 잘라도 review_id 기반 repair로 LLM 선택 결과를 복구한다.
- dry-run 기준 두 게임 모두 `llm_success_rate=1.0`, `fallback_rate=0.0`으로 1게임/5게임 확대 전 Map 게이트를 통과했다.
- Reduce 출력은 실제 리뷰 근거를 포함하되, 공개 문장에서는 스포일러 고유명사를 추상화해야 한다. 예를 들어 내부 evidence가 특정 보스명이나 엔딩명을 포함하더라도 공개 출력은 "후반부 대형 보스전", "특정 엔딩 연출 이후 강제 종료"처럼 표현한다.
- Playtime bucket coverage가 부족한 경우 playtime reduce를 호출하지 않아 요청 수와 토큰 사용량을 줄인다.
- `dry_quality_run.py --assert-gates`는 Map 성공률, deterministic fallback, Reduce 토큰/요청, 오류 여부, 출력의 review_id 근거 수를 자동 판정한다.

### 7-2. 로컬 LLM Map 유지 보완안

Map 단계의 책임은 단순 요약이 아니라 원문 리뷰를 Reduce가 사용할 수 있는 고밀도 evidence JSON으로 바꾸는 것이다. 따라서 deterministic extractor만으로 Map을 끝내면 안 된다. deterministic extractor는 로컬 LLM이 사용할 원문 기반 candidate와 실패 복구값을 제공하고, 정상 경로의 최종 Map JSON은 로컬 Ollama가 생성한다.

권장 실행 순서:

1. chunk 생성 시 `review_id`, `platform_code`, `reviewer_type`, `helpful_count`, `playtime_hours`, `playtime_bucket`을 명시한다.
2. deterministic extractor가 review_id별 candidate를 만든다.
   - candidate는 원문 snippet, 키워드 기반 aspect/polarity, source metadata, playtime metadata를 포함한다.
   - candidate는 최종 결과가 아니라 LLM 입력 보조 자료다.
3. 로컬 Ollama Map prompt에는 원문 chunk와 candidate를 함께 넣는다.
   - 원문 chunk는 사실 검증의 기준이다.
   - candidate는 누락 방지와 review_id 정합성 기준이다.
   - LLM은 candidate를 그대로 복사하지 말고, 더 구체적인 detail과 대표 quote를 생성해야 한다.
4. Ollama 출력은 JSON schema validator를 통과해야 한다.
5. validator는 통과 결과를 세 등급으로 분류한다.
   - `llm_valid`: LLM JSON을 그대로 사용
   - `llm_repaired`: review_id/source는 맞지만 일부 필드가 약해 candidate로 보강
   - `deterministic_fallback`: LLM 호출 또는 JSON 품질이 실패해 candidate를 사용

Map 품질 validator 기준:

- `evidence_items`는 비어 있으면 실패다.
- `evidence_items[].review_id`는 chunk의 review_id 안에 있어야 한다.
- `source`는 chunk metadata와 일치해야 하며, Steam/Metacritic user/critic을 임의로 바꾸면 실패다.
- `detail`은 aspect명만 반복하는 문장이 아니라 원문에서 확인 가능한 상황, 감각, 조건 중 최소 2개 이상을 포함해야 한다.
- `snippet`은 원문 substring 또는 원문에서 공백만 정규화한 문장이어야 한다.
- `aspect`와 `polarity`가 candidate와 다르면 허용하되, `detail` 또는 `snippet`에서 그 근거가 확인되어야 한다.
- 같은 review_id에서 너무 유사한 evidence가 반복되면 낮은 품질 evidence로 보고 제거한다.

Map 단계 계측:

- `map_llm_valid_chunks`: LLM JSON을 그대로 사용한 chunk 수
- `map_llm_repaired_chunks`: candidate로 보강한 chunk 수
- `map_deterministic_fallback_chunks`: deterministic fallback chunk 수
- `map_json_invalid_chunks`: JSON parse 실패 chunk 수
- `map_empty_evidence_chunks`: evidence 공백 chunk 수
- `map_source_mismatch_chunks`: source/reviewer_type 불일치 chunk 수
- `map_input_tokens`, `map_output_tokens`: 로컬 Ollama 디버깅 지표로만 기록

dry-run 통과 기준:

- 1게임 dry-run: LLM valid + repaired 비율이 chunk 기준 70% 이상이어야 한다.
- 5게임 dry-run: LLM valid + repaired 비율이 chunk 기준 80% 이상이어야 한다.
- deterministic fallback만으로 통과한 결과는 운영 성공으로 보지 않는다.
- fallback 비율이 높으면 Reduce 품질 평가 전에 Map prompt, 모델, 출력 길이, candidate 포맷을 먼저 보완한다.

## 8. 기능별 Reduce 파이프라인 설계

### 8-0. 기능별 Reduce 입력 계약

현재 main은 `platform_code == "metacritic"`인 리뷰를 모두 `critic`으로 태깅한다. 따라서 문서상 "Metacritic user"를 별도 입력으로 쓰려면 크롤링/저장 단계에서 Metacritic user review와 critic review를 구분하는 필드가 먼저 필요하다.

이 필드가 추가되기 전의 기본 입력 계약은 다음과 같다.

| Reduce | 입력 소스 | 유지해야 할 보조 입력 |
|---|---|---|
| User Summary | Steam user map JSON. Metacritic user가 명시적으로 구분되는 경우에만 Metacritic user 포함 | Steam 추천률, Steam 긍정/부정 분포, 카테고리 통계, 원문 기반 대표 인용 |
| Critic Summary | Metacritic critic map JSON | Metacritic critic 평균 점수, low/mid/high bin 분포 |
| Playtime Summary | Steam user map JSON 중 early/mid/late bucket이 부착된 항목 | bucket threshold, bucket별 리뷰 수, bucket coverage fallback 여부 |
| Final Composer | User/Critic/Playtime Reduce 결과, score anchors, category stats, aspect evidence count | raw review 전문이나 긴 map chunk는 다시 넣지 않음 |

Metacritic user review를 실제로 지원하려면 `reviewer_type`이 `user|critic`으로 들어오는 데이터 계약을 먼저 만들고, `tag_reviews()`가 플랫폼만으로 critic을 판정하지 않도록 바꿔야 한다. 그 전까지는 "Metacritic user avg"는 score anchor로만 사용하고, user reduce의 본문 근거로 사용하지 않는다.

### 8-1. User Summary Reduce

입력:

- Steam user map JSON
- Metacritic user map JSON은 `reviewer_type=user`가 실제로 구분되는 경우에만 포함
- score anchors: Steam 추천 비율, Metacritic user avg
- 원문 기반 대표 quote 일부
- category_frequency 기반 상위 category와 긍정 비율

출력:

```json
{
  "summary": "...",
  "sentiment_overall": "positive|mixed|negative",
  "sentiment_score": 0,
  "pros": [],
  "cons": [],
  "keywords": [],
  "recommended_for": [],
  "caution_for": []
}
```

길이:

- `summary`: 9~12문장
- 각 문장은 가능한 한 `aspect`, 구체 상황, 평가 감각, source 근거 중 3개 이상을 포함한다.
- `pros`: 5~7개
- `cons`: 4~6개
- `keywords`: 8~12개
- `recommended_for`: 3~5개
- `caution_for`: 3~5개

저장:

- `user_summaries`
- `game_review_summaries.pros_json`, `cons_json`, `keywords_json`에 일부 반영

규칙:

- 현재 main의 Steam 추천률 anchor를 유지해 `sentiment_score`가 부정 리뷰의 긴 문장량에 과도하게 끌려가지 않도록 한다.
- 대표 인용은 Map JSON에서 모델이 생성한 문장이 아니라 원문 리뷰에서 추출한 snippet을 사용한다.
- Metacritic user가 실제 리뷰 단위로 구분되지 않으면 user 본문 근거로 넣지 않는다.
- "유저들은 전투를 칭찬했다"처럼 aspect명만 반복하는 문장은 실패로 본다. 최소한 한 문장에는 어떤 전투 상황, 어떤 감각, 어떤 조건에서 좋거나 나빴는지가 들어가야 한다.
- summary는 `evidence_items` 중 반복 빈도가 높은 detail을 우선 사용하되, 단일 리뷰의 과장된 표현을 전체 의견처럼 일반화하지 않는다.

### 8-2. Critic Summary Reduce

입력:

- Metacritic critic map JSON만 사용
- critic score anchor

출력:

```json
{
  "summary": "...",
  "sentiment_overall": "positive|mixed|negative",
  "sentiment_score": 0,
  "pros": [],
  "cons": [],
  "keywords": [],
  "evaluation_criteria": []
}
```

길이:

- `summary`: 6~8문장
- 평론가가 어떤 기준으로 평가했는지, 어떤 장점/한계를 들었는지 구체적으로 쓴다.
- `pros`: 4~6개
- `cons`: 3~5개
- `keywords`: 6~10개
- `evaluation_criteria`: 4~6개

저장:

- `critic_summaries`

규칙:

- 유저 평가와 비교하지 않는다.
- "평론가와 유저 괴리" 판단은 여기서 하지 않는다.

### 8-3. Playtime Reduce

입력:

- Steam user map JSON 중 `playtime_signals`
- early/mid/late bucket별 grouped summaries

출력:

```json
{
  "early": {"summary": "...", "sentiment_overall": "...", "sentiment_score": 0, "pros": [], "cons": [], "keywords": []},
  "mid": {"summary": "...", "sentiment_overall": "...", "sentiment_score": 0, "pros": [], "cons": [], "keywords": []},
  "late": {"summary": "...", "sentiment_overall": "...", "sentiment_score": 0, "pros": [], "cons": [], "keywords": []}
}
```

길이:

- 각 bucket summary: 3~4문장
- 세 bucket이 모두 있으면 총 9~12문장
- bucket별 `pros`: 3~5개
- bucket별 `cons`: 2~4개
- bucket별 `keywords`: 5~8개
- early/mid/late의 차이를 억지로 만들지 않고, 실제 `evidence_items`가 있는 차이만 쓴다.

저장:

- `playtime_analyses`

규칙:

- bucket 입력이 부족한 경우 해당 bucket은 `null`.
- 임의로 시간대별 감정 변화를 꾸며내지 않는다.
- 각 bucket의 summary/pros/cons/keywords도 전체 품질 기준을 만족해야 한다. "초반은 긍정적이고 후반은 부정적이다"처럼 추상적 추세만 쓰면 실패로 본다.
- bucket 간 차이는 플레이타임 구간별 실제 `evidence_items`의 차이에서만 도출한다.
- early/mid/late 중 유효 evidence가 없는 bucket만 있는 경우에는 playtime reduce 자체를 호출하지 않는다.
- 각 bucket별 최소 유효 evidence 수는 5개로 둔다. 전체 유효 bucket이 2개 미만이면 `playtime_analyses`는 부분 또는 `null` 결과로 처리한다.

### 8-4. Final Composer Reduce

입력:

- User Summary 결과
- Critic Summary 결과
- Playtime Summary 결과
- aspect 통계
- score anchors

출력:

```json
{
  "one_liner": "...",
  "aspect_scores": {
    "graphics": {"label": "...", "score": 0},
    "controls": {"label": "...", "score": 0}
  },
  "sentiment_overall": "positive|mixed|negative",
  "sentiment_score": 0,
  "pros": [],
  "cons": [],
  "keywords": [],
  "representative_review_policy": {
    "steam": "helpful + aspect coverage",
    "metacritic": "critic/user balance"
  }
}
```

길이:

- `one_liner`: 1문장, 80자 이내
- `aspect_scores`: 근거가 있는 aspect만 4~7개
- `pros`: 5~7개
- `cons`: 4~6개
- `keywords`: 8~12개
- 긴 본문을 만들지 않는다. 상세 본문 길이는 User/Critic/Playtime Reduce가 담당한다.

저장:

- `game_review_summaries`

규칙:

- raw review나 긴 map chunk를 다시 넣지 않는다.
- 앞 단계 결과 간 충돌이 있으면 score anchor와 evidence count를 우선한다.
- `summary_text`는 현재 정책대로 `null` 유지 가능. 본문은 user/critic/playtime 테이블에서 제공한다.
- 현재 main의 `one_liner`, `aspect_scores`, `pros`, `cons`, `keywords`, `representative_reviews_json` 저장 흐름을 깨지 않는다.
- final composer는 기능별 결과를 짧게 붙이는 단계가 아니라, 구체 evidence를 잃지 않도록 중복을 제거하고 문장 밀도를 높이는 단계다.
- one_liner는 전체 판정만 담당하고, 구체 근거는 user/critic/playtime 본문에 남긴다.
- final composer의 `pros`, `cons`, `keywords`, `aspect_scores.label`도 실제 evidence에서 나온 표현이어야 한다. 단순히 "그래픽", "전투", "최적화" 같은 범주명만 나열하면 실패로 본다.
- `one_liner`는 짧더라도 실제 리뷰 근거와 충돌하면 안 되며, score anchor와 evidence count를 함께 반영한다.

## 8-5. 대표 인용과 evidence 정책

대표 인용은 품질과 신뢰도에 직접 영향을 주므로 LLM 생성값에 의존하지 않는다.

- 원문 리뷰에서 aspect 키워드가 밀집된 구간을 deterministic하게 추출한다.
- 긍정 유저, 부정 유저, 비평가 인용을 균형 있게 선택한다.
- Map JSON의 `quote_candidates`는 보조 후보로만 사용하고, 최종 대표 인용은 원문 `review_id`와 연결된 snippet이어야 한다.
- Reduce 결과의 pros/cons/keywords/aspect_scores는 가능한 한 Map JSON의 `evidence_ids` 또는 원문 대표 인용과 연결되어야 한다.
- 대표 리뷰 API가 원문 텍스트를 다시 조회할 수 있도록 `representative_reviews_json`에는 review id와 platform/type 정보를 보존한다.

좋은 요약 문장과 나쁜 요약 문장의 기준:

| 구분 | 예시 | 판정 |
|---|---|---|
| 나쁨 | 유저들은 전투를 칭찬했다. | aspect만 말하고 실제 근거가 없음 |
| 나쁨 | Metacritic 유저들은 후반 반복성을 비판했다. | 현재 데이터 계약상 Metacritic user 본문 근거가 없고 표현도 추상적 |
| 좋음 | 여러 Steam 리뷰는 보스전에서 BGM이 긴박감을 키우고, 회피 후 반격하는 흐름이 전투 몰입감을 만든다고 언급했다. | 실제 리뷰 snippet에서 가져올 수 있는 상황/감각/행동 단서가 있음 |
| 좋음 | 반대로 일부 리뷰는 후반부에 비슷한 적 배치와 반복 파밍이 이어져 긴장감이 떨어진다고 지적했다. | 불만의 조건과 맥락이 구체적임 |

이 기준을 만족하려면 Reduce 입력에는 aspect label만 넣지 말고 `detail`, `snippet`, `review_id`, `source`, `polarity`를 함께 넣어야 한다.

## 9. 요청·토큰 계측 확장

요청·토큰 계측의 운영 기준은 Reduce API 사용량이다. Map 단계의 로컬 Ollama token count는 디버깅 지표로 남길 수 있지만, 일일 quota gate와 50게임/일 예산 계산에는 사용하지 않는다.

기능별 파이프라인에서는 Reduce API에 대해 다음 세부 계측을 추가한다.

```json
{
  "reduce_usage": {
    "user": {"requests": 1, "input_tokens": 2700, "output_tokens": 1100, "retry": 0},
    "critic": {"requests": 1, "input_tokens": 900, "output_tokens": 550, "retry": 0},
    "playtime": {"requests": 1, "input_tokens": 1700, "output_tokens": 900, "retry": 0},
    "final": {"requests": 1, "input_tokens": 1300, "output_tokens": 650, "retry": 0}
  }
}
```

저장 후보:

- 단기: `ReviewSummaryJob.failure_reasons_json`에 `reduce_usage` 포함
- 장기: `review_summary_jobs`에 `reduce_usage_json` 컬럼 추가

일일 한도 관리는 Reduce API 전용 Redis counter로 한다.

키 예시:

- `quota:groq:requests:2026-05-25`
- `quota:groq:tokens:2026-05-25`
- `quota:groq:tokens_minute:2026-05-25T14:31`

운영 규칙:

- `quota:*` counter에는 Reduce API의 prompt/completion tokens만 더한다.
- Map local tokens는 `ReviewSummaryJob.map_*_tokens`에만 기록하고 quota counter에는 더하지 않는다.
- dry-run 리포트에는 Map tokens와 Reduce tokens를 모두 표시하되, "한도 사용량" 표에는 Reduce tokens만 사용한다.

하드 게이트:

- 품질 최대화 모드에서는 일일 토큰이 490K를 넘으면 신규 게임 batch 중단
- 품질 최대화 모드에서는 즉시 retry 금지. 실패한 feature reduce는 다음 날 지연 큐로 이동
- 안정 운영 모드에서는 일일 토큰이 450K를 넘으면 신규 게임 batch 중단
- 안정 운영 모드에서는 475K를 넘으면 retry 금지
- 500K 도달 전 반드시 stop

입력 압축 게이트:

- User Reduce 입력 evidence는 기본 최대 24개로 시작하고, 품질이 부족한 게임에서만 50개까지 확대한다.
- Critic Reduce 입력 evidence는 기본 최대 20개로 시작하고, critic coverage가 충분할 때만 30개까지 확대한다.
- Playtime Reduce 입력 evidence는 bucket당 최대 8개로 시작하고, 전체 bucket coverage가 충분할 때만 bucket당 15개까지 확대한다.
- Playtime Reduce는 selected Steam 리뷰에서 early/mid/late 각 bucket이 최소 20개 이상 확보된 경우에만 호출한다. 이 조건을 만족하지 못하면 bucket별 부분 요약을 만들지 않고 `null`로 둔다.
- Final Composer 입력은 원문 evidence를 다시 길게 넣지 않고 feature reduce 결과와 상위 evidence anchor 10개 이하만 넣는다.
- 동일 review_id/aspect/detail 조합은 중복 제거한다.
- Reduce 호출 전 예상 prompt 길이가 목표 예산을 넘으면 낮은 품질 evidence부터 제거한다.

## 10. 구현 순서

1. deterministic evidence candidate/fallback extractor 구현
2. Map JSON schema와 validator 추가
3. `run_map_stage()`를 로컬 Ollama JSON Map 기본 경로로 전환하되, JSON 실패 시 deterministic candidate로 복구
4. 기존 문자열 Map과 JSON Map을 병행 지원하는 adapter 추가
5. evidence dedupe/compression 계층 추가
6. Reduce를 `reduce_user`, `reduce_critic`, `reduce_playtime`, `reduce_final`로 분리
7. playtime reduce 호출 조건 추가: 유효 bucket evidence 부족 시 skip/null 처리
8. `run_hybrid_summary_pipeline()`을 orchestrator로 변경
9. `ReviewSummaryJob`에 기능별 Reduce API usage 기록
10. token bucket throttler 추가
11. 기존 E2E 검증에 기능별 schema 검증 추가
12. 1게임 dry-run으로 Map JSON 성공률과 fallback 비율 측정
13. 5게임 dry-run으로 실제 평균 토큰과 품질 기준 통과율 측정
14. 측정값으로 이 문서의 추정치를 갱신

### 10-1. 스포일러 안전성 반영 체크리스트

스포일러 안전 기준을 코드에 반영할 때는 다음 파일/함수를 수정한다.

| 영역 | 파일/함수 | 수정 내용 |
|---|---|---|
| Map 프롬프트 | `ai-pipeline/ai_module/map_reduce/map_local.py` `_build_map_prompt()` | `evidence_items` schema에 `public_detail`, `spoiler_risk`, 필요 시 `spoiler_terms`를 추가한다. `detail/snippet`은 내부 검증용 원문 근거, `public_detail`은 공개 출력용 redacted 표현임을 명시한다. |
| Map retry 프롬프트 | `map_local.py` `_build_map_retry_prompt()` | retry 출력에서도 `public_detail`과 `spoiler_risk`를 유지하도록 요구한다. retry가 candidate를 복사하더라도 공개용 redaction 필드는 누락하지 않는다. |
| Map schema normalize | `ai-pipeline/ai_module/map_reduce/map_schema.py` `normalize_map_payload()` | `public_detail`, `spoiler_risk`, `spoiler_terms`를 정규화한다. `spoiler_risk`는 `none|low|medium|high`만 허용하고, `public_detail`이 없으면 `detail`을 안전하게 redaction하거나 fallback 규칙을 적용한다. |
| deterministic fallback | `map_schema.py` `legacy_text_to_map_payload()` | deterministic candidate에도 `public_detail`, `spoiler_risk` 기본값을 채운다. 키워드 기반으로 엔딩/최종 보스/반전/사망/후반부 등은 `medium` 이상으로 표시한다. |
| LLM repair | `map_schema.py` `repair_llm_payload_with_candidate()`, `repair_llm_text_with_candidate_ids()` | repair 과정에서 candidate의 `public_detail`, `spoiler_risk`, `spoiler_terms`를 잃지 않는다. LLM 출력에 raw spoiler detail만 있고 public detail이 없으면 candidate 기반 redaction을 적용한다. |
| Reduce 입력 압축 | `ai-pipeline/ai_module/map_reduce/reduce_api.py` `_evidence_subset()` | Reduce에는 `detail`보다 `public_detail`을 우선 전달한다. `spoiler_risk=medium|high`인 evidence는 raw `snippet`을 그대로 넘기지 않거나 redacted snippet만 넘긴다. |
| Reduce 프롬프트 | `reduce_api.py` `FEATURE_QUALITY_RULES`, `_build_feature_prompt()` | `named boss/area` 같은 문구를 제거한다. 대신 경험 유형, 진행 구간, 실패 조건, 감정 영향, 기술 증상처럼 스포일러 안전한 구체성을 요구한다. |
| 출력 계약 | `reduce_api.py` user/critic/playtime/final `output_contract` | summary/pros/cons/keywords/recommended_for/caution_for/evaluation_criteria가 스포일러 고유명사를 직접 노출하지 말아야 한다는 조건을 추가한다. |
| dry-run gate | `ai-pipeline/dry_quality_run.py` | `--assert-gates`에 공개 출력 스포일러 금지어 검사를 추가한다. 금지어는 단순 문자열뿐 아니라 `spoiler_risk=high` evidence의 raw term이 출력에 재등장하는지도 검사한다. |
| 단위 테스트 | `ai-pipeline/test_map_reduce_quality.py` | `public_detail` 보존, `spoiler_risk` 정규화, Reduce 입력 redaction, 공개 출력 금지어 gate를 테스트한다. |

금지어/gate의 기본 예시는 다음이다.

```text
엔딩, 최종 보스, 마지막 보스, 반전, 죽음, 사망, 배신, 정체, 후반부,
ending, final boss, last boss, plot twist, dies, death, betrayal, true identity
```

주의:

- 금지어만으로 스포일러를 완전히 판정할 수는 없다. 따라서 gate는 1차 안전장치이고, 최종 품질 평가는 샘플 출력 리뷰를 병행한다.
- "후반부" 자체는 제품 판단에 필요한 진행 구간 표현으로 허용할 수 있다. 다만 특정 후반 지역명, 엔딩명, 보스명과 결합하면 실패로 본다.
- 내부 evidence에서 스포일러 용어를 제거하면 review_id 근거 검증력이 약해진다. 원문 evidence는 보존하고, Reduce 입력과 공개 출력에서 redaction한다.

구현 중 주의:

- `tag_reviews()`가 Metacritic 전체를 critic으로 보는 현재 계약을 변경할지 여부를 먼저 결정한다.
- 변경하지 않는다면 User Reduce 문서와 코드에서 Metacritic user 입력을 제거한다.
- 변경한다면 크롤러/저장/API 전 구간에 `reviewer_type` 또는 동등한 구분 필드를 추가한다.
- 기능별 Reduce를 추가해도 현재 `score_anchors`, `category_frequency`, `representative_quotes` 입력은 제거하지 않는다.
- token bucket은 provider 호출 직전에 적용해 retry까지 포함한 실제 사용량을 제한한다.
- `qwen2.5:1.5b`는 JSON evidence 생성 성공률이 낮았으므로, 해당 모델을 계속 사용한다면 prompt, candidate 주입, 출력 길이, 재시도 정책을 먼저 보완한다.
- 보완 후에도 Map JSON 성공률이 기준에 미달하면 모델 교체 또는 모델 크기 상향을 검토한다. 이 경우에도 deterministic evidence candidate와 schema validator는 제거하지 않는다.

## 11. 성공 기준

품질 기준:

- 각 Reduce 출력이 독립 JSON schema를 통과한다.
- user/critic/playtime/final 중 하나가 실패해도 실패 범위가 분리된다.
- Map 단계는 로컬 Ollama JSON 생성 성공률을 핵심 품질 지표로 관리한다. 1게임 dry-run 기준 chunk의 70% 이상, 5게임 dry-run 기준 chunk의 80% 이상이 deterministic fallback 없이 schema를 통과해야 한다.
- 대표 리뷰와 요약이 같은 evidence id 또는 aspect 근거를 공유한다.
- playtime bucket이 있는 게임에서는 early/mid/late 중 최소 2개 이상이 실제 근거 기반으로 생성된다.
- 모든 summary 본문의 핵심 문장 중 최소 70%는 `evidence_items` 또는 원문 대표 snippet에 연결 가능해야 한다.
- pros/cons/recommended_for/caution_for/evaluation_criteria/aspect_scores.label 중 최소 70%는 대응되는 evidence id, source, 또는 category stat 근거가 있어야 한다.
- `keywords`는 단순 빈도어가 아니라 실제 리뷰 근거가 있는 topic이어야 하며, 상위 keyword의 절반 이상은 `evidence_items.aspect` 또는 `category_frequency`와 연결되어야 한다.
- "유저들은 X를 칭찬했다", "평론가들은 Y를 비판했다", "후반부 평가는 엇갈린다"처럼 detail 없는 문장이 연속되면 품질 실패로 본다.
- 좋은 문장은 aspect명, 스포일러 안전화된 구체 상황, 평가 감각, 근거 source 중 최소 3가지를 포함해야 한다. 이 기준은 user, critic, playtime, final의 모든 자연어 필드에 적용한다.
- 공개 출력에 특정 보스명, 엔딩명, 반전, 캐릭터 사망, 후반 지역명, 퀘스트 결말이 그대로 포함되면 품질 실패로 본다.
- 스포일러 위험 evidence를 완전히 버리지는 않는다. 내부 집계와 품질 검증에는 사용하되, 공개 문장에서는 경험 유형과 영향으로 추상화한다.

운영 기준:

- 50게임/일 기본 배치가 500K tokens/day 이하에서 끝난다.
- 50게임/일 기본 배치가 1,000 requests/day 이하에서 끝난다.
- 품질 최대화 모드는 retry 없이 490K tokens/day 이하이면 정상 운영, 490K 초과는 신규 batch 중단으로 처리한다.
- 안정 운영 모드는 retry 포함 평균이 475K tokens/day 이하이면 정상 운영, 475K 초과는 경고로 처리한다.
- 분당 token bucket이 30K TPM을 넘기지 않는다.

검증 기준:

- `compileall` 통과
- 기존 `demo.py --test --scenario all` 통과
- 신규 Map JSON fixture 테스트 통과
- 신규 Reduce 기능별 schema 테스트 통과
- 5게임 샘플 실행 후 `ReviewSummaryJob`의 기능별 Reduce API 토큰 사용량 리포트 생성
- Map JSON 성공률, deterministic fallback 비율, candidate 보강 비율, 품질 validator 통과율을 함께 리포트
- 1게임 dry-run에서 로컬 Ollama Map 성공률이 기준에 미달하면 5게임 확대를 중단하고 Map prompt, candidate 주입 방식, 모델 설정을 보완
- 로컬 검증 데이터가 5게임 미만이면 현재 DB에 존재하는 모든 게임을 `dry_quality_run.py --assert-gates`로 검증하고, DB의 게임 수를 함께 기록한다.

## 12. 요구사항 대응표

| 요구사항 | 설계 반영 |
|---|---|
| Map 단계 출력 JSON 구조화 | 7장에서 로컬 Ollama JSON Map 기본 경로, deterministic candidate/fallback, Map JSON schema, 검증 규칙, 실패 처리, `json_v2_llm_map` 캐시 버전을 정의 |
| Reduce 단일 프롬프트 폐지 | 8장에서 user, critic, playtime, final composer 4개 기능별 Reduce로 분리 |
| 요약 품질 최대화 | 기능별 입력을 분리하고, 대표 quote/evidence id/aspect 근거를 단계별로 유지 |
| 실제 리뷰 기반의 구체 요약 | 1장, 7장, 8-5장, 11장에서 원문 snippet과 `evidence_items` 기반의 상세 근거 보존을 품질 기준으로 정의 |
| 스포일러 안전 구체성 | 1-1장과 11장에서 내부 evidence 보존과 공개 출력 redaction을 분리하고, 특정 보스명/엔딩명/후반 지역명/반전/캐릭터 사망/퀘스트 결말 노출을 실패 기준으로 정의 |
| 현재 main 품질 장치 보존 | 2-1장에서 언어 필터, fallback, bucket coverage, score anchor, category stats, 대표 인용을 유지 조건으로 정의 |
| 일일 요청 횟수 트레이드오프 | 5장에서 1회, 4회, 5~6회 Reduce 대안을 비교 |
| 일일 토큰 사용량 트레이드오프 | 4장과 5장에서 Reduce API 기준 490K/day 품질 최대화안, 430K/day 안정 운영안, 9.8K~12K 고품질안을 비교 |
| 하루 50개 게임 처리 | 3장, 4장, 6장에서 50게임 기준 RPD/TPD/TPM 예산과 처리 시간을 산정 |
| 한도 준수 | 9장에서 Redis counter와 490K/450K/475K/500K hard gate를 정의 |
| 구현 가능한 전환 순서 | 10장에서 schema, adapter, 기능별 reduce, orchestrator, 테스트 순서를 정의 |

## 13. 결론

50게임/일 목표에서는 기능별 Reduce를 무제한 분리할 수 없다. 요청 수 한도는 여유가 있지만, 토큰 한도가 실질 병목이다.

권장안은 다음이다.

- Map은 로컬 Ollama JSON evidence 생성을 기본 경로로 구성
- deterministic evidence는 로컬 LLM의 입력 candidate, 검증 기준, 실패 시 fallback으로 사용
- Map 출력은 JSON 구조화하되, LLM JSON 생성 실패 시 원문 기반 evidence candidate로 복구
- Reduce는 4개 기능 파이프라인으로 분리
  - user summary
  - critic summary
  - playtime summary
  - final composer
- 게임당 목표 외부 사용량은 `4 requests`, `9.8K tokens`
- 50게임/일 기준 `200 requests`, `490K tokens`
- 즉시 retry는 허용하지 않고 실패 항목은 다음 날 지연 큐로 넘긴다.
- 챗봇 또는 수동 재생성과 한도를 공유해야 하는 날에는 안정 운영 모드인 `8.6K tokens/game`, `430K tokens/day`로 낮춘다.

이 설계가 현재 한도 안에서 품질 개선 폭과 운영 안정성의 균형이 가장 좋다.
