# 제품 품질 증명 — 정량 실험 (§3)

캡스톤 최종 발표 "3. 제품 품질 증명"을 뒷받침하는 정량 실험의 스크립트·원자료·결과 폴더.
핵심 논지: **요약 품질은 모델이 아니라 파이프라인 설계(근거 기반 추출·검증·결정론 점수)에서 나온다.**

## §3 4층 구조

품질을 4개 층으로 누적 증명한다 — 아래로 갈수록 강한 주장.

| 층 | 질문 | 폴더 | 상태 |
|---|---|---|---|
| **1층 — 안전 바닥** (faithfulness) | 요약이 근거에 충실한가? 그 지표는 믿을 만한가? | `layer1_faithfulness/` | ✅ 완료 |
| **2층 — 안정성** (결정성) | 같은 입력 → 같은 점수인가? | `layer2_determinism/` | ✅ 완료(5/7) |
| **3층 — 정합성** (점수 정합) | 결정론 점수가 실제 리뷰 데이터와 일치하는가? | `layer3_alignment/` | ✅ 완료 |
| **4층 — 유용성** (선호 비교) | 실제로 더 유용한 요약인가? | `layer4_usefulness/` | ⬜ 향후 |

> 구 "실험 2 (ablation)"은 **폐지**됨 → `_deprecated_ablation/` (사유: `DEPRECATED.md`). 그 의도는
> 3층(정합성)이 더 정직·강하게 대체한다(베이스라인이 못 만드는 결정론 점수의 데이터 정합성).

## 원칙 (중요)

- **기존 코드 변경 금지**: `ai_module/`·`backend/`의 기존 로직은 import만 하고 수정하지 않는다.
- **요약 데이터 변경 금지**: `game_review_summaries` 등 라이브 테이블·Redis를 건드리지 않는다.
  payload는 `force` 엔드포인트가 아니라 **오프라인 파이프라인 직접 호출**(DB 미영속)로 캡처한다.
- **DB는 읽기 전용**, **모든 산출물은 이 폴더에만 기록**.

## 코어 평가셋 (전 층 공용)

`core_eval_set.csv` — 20개. 선정 스크립트: `select_core_eval_set.py` (시드 20260607).
층화 기준: ① faithfulness 저/중/고 ② 장르(Steam 태그) ③ 리뷰량 소/중/대. 배분: 저 5 · 중 8 · 고 7.

## 공통 재료 — payload

각 층 실행 전, 코어셋 게임의 `reduce_payload`(+최종 요약)를 오프라인 캡처(`build_eval_payloads.py`,
컨테이너, Map=groq)해 `payloads/`에 저장(gitignore). 라이브 무손상. 현재 7개 확보(11·12·20·32·79·94·99).

## 통계 원칙

- 쌍 비교(paired) 우선, LLM 채점자 노이즈 감안해 효과크기 + 검정(Wilcoxon 등) 병기.
- 표본 크기보다 **선정 정당성·변수 통제** 우선. 시드·기준을 결과 상단에 명시.
- **양날 결과도 숨기지 않고 그대로 보고.**

## 결과

- **1층 faithfulness: ✅ 완료** — `layer1_faithfulness/`
  - mismatched negative control: 정상 0.868 → mismatched **0.013** (−98.5%, 20/20, Wilcoxon p=7.8e-5)
  - 정상 재채점이 원본 CSV를 19/20 정확 재현 → 지표가 grounding을 실제 측정함 입증
  - 저점 정성검토 + 한계(judge 변동·잔여 누출): `layer1_faithfulness/ANALYSIS.md`
  - **확장(출력별 헤드라인, 20개, Groq 0/Gemini만)**: 평론가 요약 **0.942** · 유저 요약 **0.906**(전체 근거)
    · 통합 0.931 — 셋 다 높음. 유저는 60-cap에서 0.745였으나 cap 편향 확인 후 전체 근거 재채점 → 0.906(중앙 0.975).
    전체 근거로도 낮은 5게임(Death Stranding 0.57 등)은 실제 근거 약화. mismatched는 통합서 갈음.
  - 1층 분석은 `layer1_faithfulness/ANALYSIS.md` 하나로 통합(A 헤드라인 + B mismatched + C 평론가/유저)
- **2층 결정성: ✅ 완료(5/7)** — `layer2_determinism/`
  - 같은 payload로 reduce 5회 반복: sentiment std 평균 **0.98/100**, aspect std 평균 **0.12/10**, 2/5 게임 총점 완전 불변
  - 결론: 결정론 코드 baseline 안정 + LLM 검증 delta의 작고 경계 안 변동(≠완전 동일). Map 고정 조건의 reduce 재현성(end-to-end 별도): `layer2_determinism/ANALYSIS.md`
  - 94·99는 Groq 일일 한도로 미완(패턴 일관, 확장 가능)
- **3층 정합성: ✅ 완료** — `layer3_alignment/`
  - 총점 정합 **0.909** · 카테고리 정합 **0.842** (coverage 1.00, 코어셋 20개, LLM 없이 순수 산술)
  - 베이스라인이 못 만드는 결정론 점수의 데이터 정합 입증 → 폐지된 ablation의 의도를 대체
  - 실데이터 validate 4종 통과(게임별 18/20; 2건은 균형분포 게임의 negctrl 절대임계 한계, 지표 결함 아님)
  - 저정합 정성검토(SF6 과대평가 등)·한계: `layer3_alignment/ANALYSIS.md`
- **4층 유용성: ⬜ 향후** — `layer4_usefulness/`
