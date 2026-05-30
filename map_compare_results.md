# Map 단계 비교 테스트 — GPU qwen2.5:7b vs CPU qwen2.5:1.5b

대상: Elden Ring (id=1), Grand Theft Auto V (id=2)
Reduce: Groq scout (양쪽 공통, 고정)
측정일: 2026-05-30

## 환경
- GPU: NVIDIA RTX 5060 Ti 16GB
- ollama-map: flash attention + KV q8 (map.yml)

---

## 테스트 1 — 볼륨 초기화 후 클린 E2E (GPU qwen2.5:7b)

볼륨 초기화: postgres+redis 볼륨 down -v (102게임 삭제). ollama 모델 볼륨은 보존(캐시일 뿐, Redis 청크캐시는 초기화됨).
재크롤: Steam(ko/en/zh) + Metacritic(critic). Map=GPU qwen2.5:7b, Reduce=Groq scout.

| 항목 | Elden Ring (id=1) | GTA V (id=2) |
|---|---|---|
| 입력 리뷰 (steam/critic) | 275 / 63 | 198 / 57 |
| Map 표본 리뷰 | 200 | 152 |
| Map 청크 수 | 83 | 34 |
| **Map 파이프라인 wall** | **1777s (~29.6분)** | **740s (~12.3분)** |
| 청크당 평균 | ~21.4s | ~21.8s |
| Map 토큰 in/out | 141289 / 68960 | 62776 / 28901 |
| Reduce 입력 리뷰 | 338 | 255 |
| Reduce 토큰 in/out | 12471 / 2806 | 11670 / 2198 |
| 요약 version | 1 | 1 |
| one_liner | ✓ 몰입감/시간 | ✓ 오픈월드 자유도/스토리 |
| pros / cons | 5 / 3 | 5 / 1 |
| aspect 점수 | content7.4 controls6.6 gameplay7.9 difficulty7.5 optimization6.3 | content8.8 controls5.6 gameplay7.3 graphics7.1 optimization6.7 |
| sentiment | positive 93.0 | positive 87.0 |

품질 메모: 구조 완전(one_liner/pros/cons/aspect/sentiment 모두 채워짐). 감성 93/87 = steam 추천률 앵커와 정합. reliability 정량지표(schema_compliance·hallucination_score 등)는 None — eval 모듈(Gemini/semantic) 미가동 상태.

청크 수 차이: Elden 200리뷰→83청크 vs GTA 152리뷰→34청크. Souls 리뷰 길이↑(버킷별 char 청킹에서 청크 多) 추정.

---

## 테스트 2 — CPU 환경 qwen2.5:1.5b

볼륨 유지(T1과 동일 리뷰). ollama 컨테이너를 GPU→CPU 전환(--gpus 제거, DeviceRequests=null), 동일 모델 볼륨 재사용. Map=CPU qwen2.5:1.5b, Reduce=Groq scout(동일).

| 항목 | Elden Ring (id=1) | GTA V (id=2) |
|---|---|---|
| 항목 | Elden Ring (id=1) | GTA V (id=2) |
|---|---|---|
| Map 표본 / 청크 | 200 / 83 | 152 / 34 |
| **Map 파이프라인 wall** | **4290s (~71.5분)** | **1974s (~32.9분)** |
| 청크당 평균 | ~51.7s | ~58.1s |
| Map 토큰 in/out | 142991 / 96280 | 62776 / 44041 |
| Map JSON 실패 청크 | 2 (chunk19 증거누락, chunk41 문자열미종료)→fallback | 0 |
| 요약 version | 2 | 2 |
| pros / cons | 5 / 4 | 5 / 3 |
| aspect 점수 | content8.1 controls6.6 gameplay7.9 graphics7.1 difficulty8.9 | content9.0 controls6.7 gameplay7.5 optimization6.7 |
| sentiment | positive 93.0 | positive 91.0 |
| Reduce 토큰 in/out | 13798 / 2917 | 12584 / 2463 |

---

## 비교 정리

### 속도 (동일 입력·동일 청크 수)
| 게임 | T1 GPU 7b | T2 CPU 1.5b | 배율 |
|---|---|---|---|
| Elden (83청크) | 1777s (~21.4s/청크) | 4290s (~51.7s/청크) | **2.41× 느림** |
| GTA (34청크) | 740s (~21.8s/청크) | 1974s (~58.1s/청크) | **2.67× 느림** |

→ **모델 크기를 7b→1.5b로 줄였는데도 CPU가 GPU보다 2.4~2.7배 느림.** GPU 부재 페널티가 모델 축소 이득을 압도. 청크당 CPU 1.5b ≈ 52~58s (목표 <30s 크게 초과).

### 토큰
- in 토큰 동일(같은 프롬프트). out 토큰은 1.5b가 7b보다 큼 (Elden 68960→96280, GTA 28901→44041). 작은 모델이 더 장황/재시도 → reduce 입력비용 소폭↑.

### 품질
- **구조 완전성**: 양쪽 모두 one_liner·pros·cons·aspect·sentiment 채움. 1.5b도 형식 붕괴 없음(스키마 fallback이 방어).
- **JSON 안정성**: 1.5b Elden에서 2청크 JSON invalid→deterministic fallback (증거 손실 소량). 7b는 0. GTA는 양쪽 0.
- **감성 앵커**: 7b/1.5b 모두 앵커 기반(Elden 93 동일, GTA 87→91 미세 변동). 큰 왜곡 없음.
- **aspect 일관성**: 모델 간 점수 드리프트 존재. Elden difficulty 7.5→8.9, content 7.4→8.1; GTA aspect 5→4개로 커버리지 축소(graphics 누락). 1.5b가 evidence 추출력 약해 aspect 커버리지·점수 안정성 떨어짐.
- one_liner는 양쪽 동일 문구(temp 0 + baseline anchor 효과).

### 결론
- **CPU 1.5b는 클라우드(무 GPU) 폴백으로 동작은 하나 느리고(청크당 ~52~58s) aspect 품질 약화.** 7b GPU가 속도·품질 모두 우위.
- 클라우드 CPU 배포가 불가피하면 1.5b로 "돌아는 가지만" 게임당 30~70분 + aspect 커버리지 손실 감수해야 함. 실사용엔 로컬 GPU map(현 아키텍처) 유지가 타당.
- 개선 여지: CPU에서 청크 수 축소(max_chars 상향으로 청크 통합) 또는 더 적은 표본으로 시간 단축 가능. 1.5b JSON 안정성은 fallback이 방어 중.

### 측정 조건
- Reduce는 양 테스트 공통(Groq scout, temp 0). 입력 리뷰·청킹 동일 → map 모델/디바이스만 변수.
- ollama: T1 GPU(flash+KV q8), T2 CPU(--gpus 제거, flash/KV 미적용). 모델 볼륨 공유.
- 정량 reliability(schema/hallucination)는 eval 모듈 미가동으로 N/A. map→precomputed-reduce 경로라 review_summary_chunks 미영속(한글누출 지표 N/A).
