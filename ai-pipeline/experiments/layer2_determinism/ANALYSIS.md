# Layer 2 분석 — 결정성/재현성 (reduce 리플레이)

**질문**: 같은 입력에 같은 점수가 나오는가?

**방법**: 저장된 reduce payload(= Map 결과 고정)를 그대로 Reduce에 **5회 반복**  → 매 회의
sentiment_score(0~100)·aspect 점수(0~10) 분산 측정.

- 표본: 코어셋 중 payload 보유 게임. **5개 완료**(11·12·20·32·79). 원자료 `determinism_matrix.csv`, 요약 `determinism_summary.csv`.

## 1. 결과 — 변동은 작고 경계 안 (완전 0은 아님)

| 게임 | sentiment 평균 | sentiment std | aspect std 평균 | aspect std 최대 |
|---|---|---|---|---|
| 12 Witcher 3 | 92.0 | **0.00** | 0.062 | 0.21 |
| 32 Death Stranding | 82.0 | **0.00** | 0.039 | 0.16 |
| 79 Dead by Daylight | 58.4 | 1.20 | 0.112 | 0.46 |
| 20 Jedi: Fallen Order | 75.8 | 1.17 | 0.132 | 0.49 |
| 11 Dragon Age: Veilguard | 52.0 | 2.53 | 0.236 | 0.75 |

- **집계**: sentiment_score std 평균 **0.98 / 100** (±~1점), aspect 점수 std 평균 **0.12 / 10** (±~0.12).
- **2/5 게임은 총점이 5회 내내 완전 불변**(std 0). 나머지도 ±1~2.5점 수준.

## 2. 해석 — "코드 baseline 안정 + 검증된 LLM delta의 작은 변동"

- 점수의 **결정론 baseline**(evidence_count·polarity로 코드가 산출)은 안정적이다. 변동은
  reduce LLM이 제안하는 **검증된 delta**(sentiment_score_delta ∈ [−8,+8], aspect 상대조정)가
  매 회 조금씩 달라서 생긴다.
- 움직이는 항목은 **게임마다 다르고 특정 항목이 고정적으로 아님** — 근거가 팽팽한(경계선) 주장에서만
  delta가 흔들린다. 그래서 **호불호작(Veilguard)이 가장 큰 변동**(content 0.75 등), 평가가 한쪽으로
  쏠린 게임(Witcher 호평·Death Stranding)은 총점이 완전히 고정된다.
- → 정직한 결론: **"같은 입력 → 똑같은 점수"는 과한 표현.** 정확히는 **"근거 고정 시 점수는
  거의 결정적이며, 변동은 작고 경계 안(총점 ±~1/100, 항목 ±~0.12/10)"**. ARCHITECTURE §14의
  주장을 이 수치로 **보강하되 한정**한다.

## 3. 범위의 한정 (중요)

- 본 실험은 **Reduce 단계만** 반복했다(Map=근거 고정). 점수를 산출하는 로직이 Reduce에 있어
  설계 주장과 1:1로 맞지만, **end-to-end(원문→Map→Reduce) 재현성은 아니다.**
- Map도 LLM이라 근거 추출이 매번 약간 다르므로, 원문부터 통째로 반복하면 변동은 **이보다 커진다.**
  즉 본 수치는 **최선의 경우(하한)**이며, 발표 시 "근거 고정 시" 조건을 명시한다.

## 4. 발표 한 줄 (2층)
> 같은 근거(payload)로 Reduce를 5회 반복하면 총점 표준편차 평균 ~1점(0~100)·항목 점수 ~0.12(0~10),
> 2/5 게임은 총점 완전 불변. 즉 결정론 코드 baseline은 안정적이고 변동은 LLM의 검증된 delta에서만
> 작게·경계 안에서 발생한다. (Map 고정 조건의 reduce 재현성.)