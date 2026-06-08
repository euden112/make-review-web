# 실험 2 — 파이프라인 vs 단순 베이스라인 (ablation)

- 표본: 코어셋 20개 | judge: gemini-3.1-flash-lite | 근거: 동일 입력 표본(파이프라인이 추린 ~200개)
- 통제: 최종 모델 llama-4-scout 동일 · 입력 표본 동일 · 베이스라인 temp=0.2 고정

## 1. faithfulness (충실도)

- 평균 — 파이프라인 **0.884** vs 베이스라인 **0.969**
- 파이프라인 우세 4 / 베이스라인 우세 10 / 동률 6 (n=20)
- Wilcoxon: W=22, p=0.05529 | 효과크기 dz=-0.520

> 주의: 베이스라인은 원문을 그대로 베끼는 경향이 있어 faithfulness가 높게 나올 수 있다. 충실도만으로 우열을 논하지 말고 아래 보조 축과 함께 본다.

## 2. 보조 축 — 스포일러 누출 수 (설계의 redaction 효과)

- 총 스포일러 용어 — 파이프라인 **0** vs 베이스라인 **0**
- 베이스라인이 더 누출한 게임 수: 0/20
- Wilcoxon(스포일러): W=n/a, p=n/a

파이프라인은 public_detail redaction으로 보스명·엔딩·반전 등을 가린다. 단순 베이스라인은 그 장치가 없어 내러티브 게임에서 스포일러가 그대로 노출된다.

## 게임별

| id | 게임 | 파이프 faith | 베이스 faith | Δfaith | 파이프 스포 | 베이스 스포 |
|---|---|---|---|---|---|---|
| 12 | The Witcher 3: Wild Hunt | 0.7143 | 1 | -0.2857 | 0 | 0 |
| 79 | Dead by Daylight | 0.6 | 1 | -0.4 | 0 | 0 |
| 34 | A Plague Tale: Innocence | 1 | 1 | 0 | 0 | 0 |
| 99 | Balatro | 0.6364 | 0.8889 | -0.2525 | 0 | 0 |
| 32 | Death Stranding | 0.625 | 1 | -0.375 | 0 | 0 |
| 11 | Dragon Age: The Veilguard | 0.9 | 1 | -0.1 | 0 | 0 |
| 53 | Star Wars Jedi: Survivor | 0.8889 | 1 | -0.1111 | 0 | 0 |
| 74 | Dave the Diver | 0.8889 | 1 | -0.1111 | 0 | 0 |
| 67 | Clair Obscur: Expedition 33 | 1 | 1 | 0 | 0 | 0 |
| 86 | TEKKEN 8 | 0.9375 | 1 | -0.0625 | 0 | 0 |
| 31 | Forza Horizon 5 | 0.7 | 1 | -0.3 | 0 | 0 |
| 94 | Overwatch 2 | 0.7857 | 1 | -0.2143 | 0 | 0 |
| 59 | Monster Hunter Rise | 1 | 0.875 | 0.125 | 0 | 0 |
| 20 | Star Wars Jedi: Fallen Order | 1 | 0.8889 | 0.1111 | 0 | 0 |
| 39 | God of War Ragnarök | 1 | 1 | 0 | 0 | 0 |
| 49 | Hitman World of Assassination | 1 | 1 | 0 | 0 | 0 |
| 70 | Dragon Age: Inquisition | 1 | 1 | 0 | 0 | 0 |
| 75 | Battlefield 2042 | 1 | 1 | 0 | 0 | 0 |
| 78 | The Outer Worlds | 1 | 0.8182 | 0.1818 | 0 | 0 |
| 87 | Street Fighter 6 | 1 | 0.9167 | 0.0833 | 0 | 0 |
