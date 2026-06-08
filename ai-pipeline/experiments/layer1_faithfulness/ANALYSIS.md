# Layer 1 (안전 바닥) 종합 분석 — faithfulness

요약이 **근거에 충실한가**, 그리고 **그 지표를 믿을 수 있는가**를 본다. 네 측정으로 구성:
**(A) 헤드라인**(통합 요약이 얼마나 충실한가) · **(B) mismatched 검증**(지표가 진짜 충실도를
재는가=타당성) · **(C) 출력별 확장**(평론가·유저 요약 각각의 충실도) · **(D) Map 단계 충실도**
(reduce의 토대인 로컬 Map 추출이 원문에 충실한가).

- judge: Gemini · 표본: 코어셋 20개
- 원자료: `mismatched_results.csv`(B), `critic_user_faithfulness.csv`(C),
  `map_fidelity_results.csv`·`map_fidelity_precise_results.csv`(D)
- 코드: `mismatched_control.py`(B), `eval_critic_user_faith.py`+`fetch_critic_user_data.py`(C),
  `build_map_fidelity_data.py`·`build_map_fidelity_precise.py`+`fetch_review_texts.py`+`score_map_fidelity.py`(D),
  통합 헤드라인(A)은 기존 `ai-pipeline/eval_ragas_faithfulness.py`(0.931 생산)

---

## A. 헤드라인 — 통합 요약 faithfulness

- **100개 평균 0.931 · 중앙값 1.000** (기존 측정, reduce-evidence 근거).
- 채점 대상은 통합 요약의 **한 줄 평 + 장점 + 단점** 텍스트.

## B. mismatched 검증 — 지표는 grounding을 실제로 본다

요약은 그대로 두고 RAGAS context만 **다른 게임 근거**로 바꿔치기(시드 고정 derangement). 지표가
grounding을 본다면 점수가 붕괴해야 한다.

| 조건 | 평균 faithfulness |
|---|---|
| 정상 (자기 근거) | **0.868** |
| mismatched (타 게임 근거) | **0.013** |
| 하락 | **0.856** (상대 −98.5%), **20/20 전부**, Wilcoxon W=0, **p=7.8e-5** |

- 근거를 바꾸자 사실상 0으로 붕괴 → **지표는 "요약이 제시된 근거에 지지되는가"를 측정**한다.
  헤드라인 0.931이 노이즈가 아니라 의미 있는 수치임을 보증.

### B-1. 한계 (정직 보고)

2. **잔여 누출 (Death Stranding, id 32)**: mismatched에서도 0.25 잔존. 요약이 모호한 메타
   진술이라 무관 근거로도 일부 "지지" 판정 → 모호한 요약일수록 근거 판별이 어려움.

## C. 출력별 확장 — 평론가·유저 요약 faithfulness

헤드라인을 유저/평론가 요약으로 확장,

| 출력 | faithfulness 평균 | 중앙값 |
|---|---|---|
| **평론가 요약** | **0.942** | 1.000 |
| **유저 요약** | **0.906** | 0.975 |
| (참고) 통합 헤드라인 | 0.931 | — |

- **세 출력 모두 높은 충실도.** 

## D. Map 단계 충실도 — 토대 검증

A·C는 reduce 산출물(요약·점수)의 충실도를 본다. 그러나 그 토대는 **로컬 Map(gemma)이 리뷰에서
근거를 추출하는 단계**다. Map이 환각하거나 출처를 잘못 붙이면 reduce가 아무리 충실해도 그 오류를
상속한다. 그래서 Map 출력 자체의 충실도를 직접 잰다.

채점 대상은 저장된 reduce payload 청크의 **Map 합성 주장**(evidence_items·critic_signals·aspect
pros/cons)이며, 두 방식으로 대조한다.

| 방식 | context(근거) | 잡아내는 것 | 평균 | 중앙값 |
|---|---|---|---|---|
| **Floor** | 게임 전체 리뷰 풀 | "어떤 리뷰에도 없는 말을 지어냈는가"(환각) | **0.941** | 1.000 |
| **정밀** | 청크의 **실제 출처 review_id 원문만** | 환각 + **오귀속**(다른 리뷰가 한 말을 잘못 귀속) | **0.940** | 1.000 |

- 표본 80청크(코어셋 20게임 × claim 많은 청크 4개). 정밀은 출처 review_id를 원문에 100% 매칭
  (결손 0/460)해 coverage-miss를 구조적으로 제거.
- **두 방식이 0.94로 수렴.** Map은 reduce(0.931)·평론가(0.942)·유저(0.906)와 동급으로 충실하다.
  ≥0.9 청크가 90%(72/80), 완전 일치(1.0)가 66%(53/80).

### D-1. 정밀이 밝힌 것

1. **Floor 저점은 coverage-miss였다.** Floor에서 낮던 OW2(0.23)·BF2042(0.36)·Dave(0.63)는
   정밀에서 0.92·1.0·0.88로 복원 — 출처가 전체풀 표본 밖이라 생긴 가짜 미지지였다.
2. **오귀속은 0건.** 정밀에서 0에 가까운 청크(GoW·Clair Obscur·Balatro 등)는 전수 수동검증
   결과 Map 주장이 출처 리뷰에 그대로 존재 = **채점기(RAGAS NLI) false-negative**다(한글 NLI
   취약 + 영어 1건 경성 실패). concat 아티팩트 1건(Forza)은 주장 정제 후 0.16→0.95로 복원.

### D-2. 한계 (정직 보고)

- 잔여 저점 3건은 Map 결함이 아니라 **채점기 측 노이즈**다. 이를 제외하면 정밀 평균은 0.971로
  오르지만, 본 표에는 보정 없이 0.940을 그대로 싣는다(판정 함수 미변경 원칙).
- 정밀의 원문(`review_texts_full20.json`)은 DB에서 읽기 전용으로 1회 덤프한 것으로, 재현 시
  `fetch_review_texts.py`로 재생성한다(원문 raw는 저장소에 싣지 않음).

---

## 발표 한 줄 (1층)
faithfulness는 근거를 바꿔치기하면 0.868→0.013(−98.5%, 20/20, p<1e-4)으로 붕괴한다 — 점수가 근거를 바탕으로 메겨진다는 증거. 
그 위에서 통합 0.931·평론가 0.942·유저 0.906(전체 근거)로 세 출력 모두 근거에 충실하며, 그 토대인 Map 추출도 0.940(환각·오귀속 검출, 중앙값 1.0)으로 동급으로 충실하다.