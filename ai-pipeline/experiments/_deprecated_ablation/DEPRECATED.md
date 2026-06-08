# ⛔ 폐지(DEPRECATED) — 구 실험 2 (ablation: 파이프라인 vs 단순 베이스라인)

2026-06-07 폐지. 발표 §3 구성에서 제외한다. 코드·결과는 기록용으로 보존만 한다.

## 폐지 사유
faithfulness 축에서 단순 단일프롬프트 베이스라인이 오히려 더 높게 나왔고(0.969 vs 0.884),
이는 "원문을 베끼면 충실도가 쉽게 오른다"는 지표 한계(Layer 1에서 이미 입증)를 재확인할 뿐,
설계 우위를 **이 축으로는** 보이지 못했다. 설계 고유 가치(결정론 점수·항목/구간/지역 분리)는
이 실험이 측정하지 못했다.

→ 그 "결정론 점수가 실제 데이터와 정합하는가"는 **Layer 3(정합성, `../layer3_alignment/`)**에서
   베이스라인이 구조적으로 만들 수 없는 산출물로 직접 증명한다. 즉 ablation의 의도는 Layer 3가
   더 정직하고 강하게 대체한다.

## 보존 파일
- `gen_baseline.py`, `score_ablation.py` — 재현 스크립트
- `ablation_results.csv`, `ablation_report.md`, `ANALYSIS.md` — 결과·분석(양날 정직 보고)
- `baseline_data.json` — 원자료(gitignore 대상일 수 있음)
