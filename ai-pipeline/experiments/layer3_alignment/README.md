# Layer 3 — 점수 정합성 (score alignment)  ✅ 완료

> **결과(2026-06-07)**: 총점 정합 0.909 · 카테고리 정합 0.842 (coverage 1.00, 코어셋 20개).
> 실데이터 validate 4종 통과(게임별 18/20). 상세: `ANALYSIS.md` · 원자료 `alignment_results.csv` · 리포트 `alignment_report.md`.
> 실행: `score_alignment_eval.py`(어댑터만 우리 스키마로 수정, 핵심 함수 불변) + `run_alignment.py`(라이브 summary + DB reviews).

§3 4층 구조의 3층. **결정론적 점수 산출물(총점·항목별 점수)이 실제 리뷰 데이터와 일치하는가**를
검증해 설계 가치를 증명한다. 베이스라인(단일 프롬프트)은 결정론적 항목 점수를 **구조적으로 만들지
못하므로** 이 축에선 비교 자체가 성립하지 않는다 → 파이프라인 고유 가치.

핵심 논지: "요약 품질은 모델이 아니라 파이프라인 설계에서 나온다."

## 상태
- 평가 모듈 `score_alignment_eval.py`는 **별도로 붙여넣을 예정**(외부 LLM 호출 없음, stdlib only,
  순수 산술·결정론). 두 축: ① 총점 정합성 ② 카테고리 정합성. 자체 신뢰도 검증 validate() 포함
  (sanity·negative control·random·monotonicity).
- 모듈이 들어오면 아래 "선행 확인 결과"에 맞게 **어댑터(load_from_reduce_payload)만** 수정한다.
  핵심 지표 함수(compute_*, evaluate, validate)는 건드리지 않는다.

## 선행 확인 결과 (2026-06-07, 실제 payload `experiments/payloads/game_79_*.json` 기준)

### ① 총점 정합성 — payload에 완비 ✅
- 요약 총점: `final_summary.sentiment_score` (0–100). 예: DbD = 59
- 원본 추천율: `reduce_payload.score_anchors.steam_recommend_ratio` (0–100). 예: DbD = 59.33
  - (보조 앵커: `metacritic_critic_avg`, `metacritic_user_avg`)
- → 어댑터는 두 값을 payload에서 직접 읽으면 됨. **STAR_MAX/매핑 주의**: sentiment_score·추천율 모두
  0–100 스케일. "추천율 p% ↔ 별점 5·(p/100)" 선형 매핑 전제이면, sentiment_score도 /20로 별점 환산.

### ② 카테고리 정합성 — 항목 점수는 있음, 추천율은 프록시/DB 필요 ⚠️
- 항목 점수: `final_summary.aspect_scores[cat].score` (0–10). → `CATEGORY_MAX=10`.
- **per-review is_recommended·카테고리 태그는 reduce_payload에 없음**(0건 확인).
- 대안 두 가지:
  - **(권장) payload 내 프록시**: `aspect_scores[cat].polarity_mix = {positive, mixed, negative}` 카운트가
    payload에 있음. 카테고리 추천율 ≈ positive/(positive+negative) 또는 positive/(pos+mix+neg)로 산출
    가능(결정론·DB 불필요). `evidence_count`/`mention_count`/`mention_share`도 coverage에 활용.
  - **(대안) DB 직접**: `external_reviews.review_categories_json` + `is_recommended`로 카테고리별 실추천율
    산출. 더 정밀하나 어댑터가 DB를 읽어야 함.
- → 어댑터는 위 중 하나로 "카테고리 추천율"을 만든다. 핵심 지표 함수는 불변.

### 카테고리 매핑 — 전부 존재 ✅
모듈 CATEGORIES → payload aspect 키:
- 콘텐츠 양 → `content` (label "콘텐츠/볼륨")
- 조작감 → `controls`
- 가성비 → `price_value`
- 그래픽 → `graphics`
- 최적화 → `optimization`
(payload엔 그 외 difficulty·gameplay·sound·story도 존재 — 모듈 CATEGORIES만 사용.)

## 실험 계획 (모듈 붙은 뒤)
1. 코어셋 20개(`../core_eval_set.csv`, 시드 20260607 — 실험 2·3과 동일 표본) payload 로드 →
   `load_from_reduce_payload()` → `evaluate()`.
2. 결과 CSV(`alignment_results.csv`): game, total_alignment, category_alignment, macro, pos_rate,
   signed_gap, coverage + 항목별 정합도.
3. **자체 신뢰도 검증을 실데이터로 재실행**: `validate()`를 합성 리뷰가 아니라 코어셋 실데이터로 돌려
   sanity·negative control·random·monotonicity 4종이 실데이터에서도 통과하는지 확인
   (= "이 지표가 메인 증거 자격이 있는가"의 핵심).
4. 자동 리포트(`alignment_report.md`): 코어셋 평균 정합도(두 축 각각·독립), 분포, 저정합 게임 정성
   검토(점수 오류 vs 지표 오류 구분), 검증 4종 결과.

## 보고 원칙
- **두 축(총점·카테고리)을 각각 독립 보고.** 단일 점수로 평균 뭉뚱그리지 말 것(macro는 보조만).
- 베이스라인 대비 명시: 베이스라인은 결정론적 항목 점수를 못 만듦 → 이 축에서 비교 불성립 = 고유 가치.
- 양날 정직: 저정합 게임은 숨기지 말고, 지표 오류인지 실제 점수 산출 결함인지 정성 검토로 구분.

## 방어 포인트 (리포트에 명시)
- "추천율 p ↔ 별점 5p" **선형 매핑** 전제 — 투명·조정 가능(STAR_MAX/매핑 교체 가능). Steam 등급이
  별점과 비선형일 수 있다는 반론엔 "선형 매핑 사용, 조정 가능"으로 대응.
- 데이터 없는 카테고리는 **임의값 주입 없이 평균에서 제외**하고 `coverage`로 별도 보고(점수 부풀림 방지).

## 산출물 (예정)
- `score_alignment_eval.py` (어댑터·설정만 조정) · `run_alignment.py`(러너) · `alignment_results.csv`
  · `alignment_report.md`(코어셋 결과 + 실데이터 검증 4종 + 저정합 정성 검토)
