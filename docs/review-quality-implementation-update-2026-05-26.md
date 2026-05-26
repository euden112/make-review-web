# 리뷰 품질 파이프라인 구현 업데이트

작성일: 2026-05-26

## 1. 적용 범위

이번 변경은 `docs/plan-review-quality-pipeline.md`와 `docs/plan-map-model-evaluation-cpu.md`의 핵심 방향을 현재 코드에 더 엄격하게 맞추기 위한 보완이다.

핵심 원칙은 유지한다.

```text
원문 리뷰
-> Map 단계: 로컬 Ollama LLM이 Reduce가 읽기 좋은 evidence JSON 생성
-> Reduce 단계: Groq API 모델이 evidence JSON을 기반으로 최종 공개 요약 생성
```

deterministic extractor는 Map 단계를 대체하지 않는다. 로컬 LLM의 입력 후보, 검증 기준, 실패 복구값으로만 사용한다.

## 2. 주요 코드 보완

### 2-1. Map evidence의 공개 안전 필드 추가

변경 파일:

- `ai-pipeline/ai_module/map_reduce/map_schema.py`
- `ai-pipeline/ai_module/map_reduce/map_local.py`

변경 내용:

- Map evidence item에 `public_detail`, `spoiler_risk`, `spoiler_terms`를 추가했다.
- 보스명, 엔딩명, 반전, 캐릭터 사망, 후반 지역명, 퀘스트 결말처럼 공개 출력에 그대로 노출하면 안 되는 단어를 탐지한다.
- Reduce 단계가 원문 detail을 직접 복사하지 않도록 공개용 detail을 우선 사용한다.
- LLM 출력이 candidate evidence와 맞지 않으면 candidate 기반 evidence로 보정하고 `llm_grounding_repaired` warning을 남긴다.
- `reviewer_id`, `review_ids` 배열 형태처럼 작은 모델이 자주 만드는 변형도 repair 경로에서 처리한다.

### 2-2. Map 프롬프트와 캐시 버전 변경

변경 파일:

- `ai-pipeline/ai_module/map_reduce/map_local.py`
- `ai-pipeline/ai_module/map_reduce/pipeline.py`

변경 내용:

- Map 프롬프트가 공개 안전 detail과 spoiler metadata를 생성하도록 요구한다.
- 대표 quote 입력에는 `review_id=N` 앵커를 포함한다.
- Map cache version을 `json_v3_spoiler_safe_map`으로 올려 기존 JSON map 캐시와 섞이지 않게 했다.

### 2-3. 작은 CPU 모델 기준 chunk 여유 확보

변경 파일:

- `ai-pipeline/ai_module/map_reduce/chunker.py`

변경 내용:

- `OLLAMA_NUM_CTX` 기준 안전 입력 토큰을 더 보수적으로 줄였다.
- deterministic candidate와 JSON schema 프롬프트가 함께 들어가도 `qwen2.5:1.5b`가 출력 여유를 갖도록 조정했다.

### 2-4. Reduce 공개 출력 정합성 강화

변경 파일:

- `ai-pipeline/ai_module/map_reduce/reduce_api.py`

변경 내용:

- Reduce 입력에서 `public_detail`을 우선 사용한다.
- spoiler risk가 `medium` 또는 `high`인 evidence는 raw snippet 대신 공개 안전 detail을 사용한다.
- 공개 출력 sanitizer를 추가해 스포일러 용어, LLM artifact, 두루뭉실한 일반론을 제거한다.
- `pros`와 `cons`는 최종 LLM 출력값을 그대로 쓰지 않고 evidence 기반 fallback sentence로 구성한다.
- `pros`는 긍정 evidence, `cons`는 부정 또는 mixed evidence에서 채운다.
- 각 장단점 문장은 `review_id=N` 앵커를 포함하게 했다.
- 자동 review_id 재작성은 제거했다. 잘못된 앵커를 코드가 임의로 바꾸면 원문 근거가 틀어질 수 있기 때문이다.

### 2-5. dry-run gate 확장

변경 파일:

- `ai-pipeline/dry_quality_run.py`

추가 gate:

- 공개 출력 artifact 금지
- 공개 출력 spoiler leak 금지
- 두루뭉실한 일반론 금지
- pros/cons 최소 개수, 길이, review_id 앵커 검증
- review_id 앵커가 evidence text와 맞는지 검증

이 gate는 테스트 통과만 보는 것이 아니라 실제 공개 출력이 기획한 품질 기준에 맞는지 자동으로 더 많이 걸러내기 위한 장치다.

### 2-6. 단위 테스트 확대

변경 파일:

- `ai-pipeline/test_map_reduce_quality.py`

추가 검증:

- spoiler metadata 생성
- 공개 detail redaction
- Reduce 입력에서 `public_detail` 우선 사용
- dry-run spoiler/artifact/vague gate
- reviewer label과 review_id 앵커 불일치 탐지
- LLM hallucinated snippet을 candidate evidence로 보정
- vague public sentence 제거
- evidence 기반 pros/cons fallback sentence 생성

## 3. 검증 기준

커밋 전 검증 기준은 다음과 같다.

```bash
.venv\Scripts\python.exe -m compileall ai-pipeline
.venv\Scripts\python.exe -m pytest ai-pipeline\test_map_reduce_quality.py -q
docker compose exec backend python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates
```

dry-run은 DB에 리뷰가 있는 현재 게임 전체를 대상으로 한다. 현재 확인된 DB에는 `ELDEN RING`, `Grand Theft Auto V`가 있다.

## 4. 남은 제약

- 현재 Docker Ollama에는 `qwen2.5:1.5b`만 설치된 상태다.
- 따라서 `docs/plan-map-model-evaluation-cpu.md`의 후보 모델 비교는 아직 완료되지 않았다.
- 이번 커밋의 검증 범위는 baseline 모델에서 Map 로컬 LLM 경로와 Reduce 공개 출력 품질 gate가 통과하는지다.

## 5. 커밋 전 검증 결과

실행 명령:

```bash
.venv\Scripts\python.exe -m compileall ai-pipeline
.venv\Scripts\python.exe -m pytest ai-pipeline\test_map_reduce_quality.py -q
docker compose exec backend python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates
```

결과:

- `compileall`: 통과
- `test_map_reduce_quality.py`: 25 passed
- 2게임 dry-run: exit code 0
- 대상 게임: `ELDEN RING`, `Grand Theft Auto V`
- 두 게임 모두 `gate_results.passed=true`

최종 dry-run 기준:

| 게임 | 리뷰 수 | chunks | Map LLM success | deterministic fallback | Reduce requests | Reduce tokens | gate |
|---|---:|---:|---:|---:|---:|---:|---|
| ELDEN RING | 36 | 11 | 1.0 | 0.0 | 2 | 8,131 | passed |
| Grand Theft Auto V | 36 | 6 | 1.0 | 0.0 | 2 | 5,710 | passed |

자동 gate 통과 항목:

- Map LLM success 기준 통과
- deterministic fallback 0
- Reduce token budget 9,800 이하
- Reduce request budget 4 이하
- Reduce error 없음
- 공개 출력의 review_id grounding 기준 통과
- 공개 출력 artifact 없음
- 공개 출력 spoiler leak 없음
- 공개 출력 vague pattern 없음
- pros/cons 최소 개수, 길이, review_id 앵커 기준 통과
- review_id 앵커와 evidence text 정합성 통과

수동 확인 메모:

- 공개 출력에서 스포일러 고유명사는 redaction되어 자동 gate를 통과했다.
- `qwen2.5:1.5b`는 여전히 일부 chunk에서 malformed JSON 또는 엉뚱한 언어 출력을 만들지만, validator와 candidate grounding repair가 deterministic fallback 없이 복구했다.
- pros/cons는 근거성과 gate 기준을 만족하지만 일부 문장은 아직 원문 리뷰의 거친 표현을 많이 보존한다. 다음 품질 개선 단계에서는 raw quote를 더 자연스러운 공개 문장으로 압축하는 후처리를 강화할 필요가 있다.
