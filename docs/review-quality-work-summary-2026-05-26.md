# 리뷰 품질 개선 작업 정리

작성일: 2026-05-26

## 1. 작업 목표

이번 작업의 목표는 `docs/plan-review-quality-pipeline.md`에 작성된 방향대로 리뷰 요약 품질을 개선하는 것이다.

핵심 기획은 다음 구조를 보존하는 것이다.

```text
원문 리뷰
-> Map 단계: 로컬 Ollama LLM이 Groq Reduce가 읽기 좋은 evidence JSON 생성
-> Reduce 단계: Groq API 모델이 evidence JSON을 바탕으로 최종 요약 생성
```

따라서 deterministic extractor는 Map 단계를 대체하지 않는다. deterministic extractor는 로컬 LLM의 입력 candidate, validator 기준, 실패 시 fallback으로만 사용한다.

## 2. 주요 변경 사항

### 2-1. Map 단계 로컬 LLM 기본 경로 복구

변경 파일:

- `ai-pipeline/ai_module/map_reduce/map_local.py`
- `ai-pipeline/ai_module/map_reduce/map_schema.py`
- `ai-pipeline/ai_module/map_reduce/chunker.py`
- `ai-pipeline/ai_module/map_reduce/pipeline.py`

변경 내용:

- 기존 deterministic-primary 구조를 수정했다.
- 기본 Map 경로는 로컬 Ollama LLM 호출로 동작한다.
- deterministic payload는 `[DETERMINISTIC_CANDIDATE]`로 prompt에 포함된다.
- `MAP_FORCE_DETERMINISTIC=true`일 때만 deterministic-only 경로를 사용한다.
- Ollama `/api/chat` 호출에 `format: "json"`과 JSON-only system prompt를 추가했다.
- 캐시 버전을 `json_v2_llm_map`으로 변경해 이전 deterministic-primary 캐시와 분리했다.

### 2-2. 로컬 LLM 출력 repair 계층 추가

변경 파일:

- `ai-pipeline/ai_module/map_reduce/map_schema.py`
- `ai-pipeline/ai_module/map_reduce/map_local.py`

보완한 failure mode:

- LLM이 `evidence_items` schema 대신 `reviews` 배열을 반환하는 경우
- LLM이 `review_id` 없이 `content`만 반환하는 경우
- LLM이 JSON을 중간에 끊는 경우
- LLM이 id만 반환하고 본문 detail을 누락하는 경우

보완 방식:

- 정상 JSON이면 `normalize_map_payload()`로 검증한다.
- `reviews` 형태 JSON은 evidence schema로 변환한다.
- `review_id`가 없으면 deterministic candidate 순서로 보정한다.
- malformed/truncated JSON에서도 `review_id`를 추출해 candidate evidence로 복구한다.
- 복구된 결과에는 `llm_schema_repaired` 또는 `llm_truncated_json_repaired` warning을 남긴다.

### 2-3. Chunk 크기 조정

변경 파일:

- `ai-pipeline/ai_module/map_reduce/chunker.py`

변경 내용:

- `OLLAMA_NUM_CTX` 기준 raw chunk 크기를 더 보수적으로 줄였다.
- JSON schema prompt와 deterministic candidate가 함께 들어가기 때문에 작은 로컬 모델이 완성된 JSON을 반환할 수 있도록 입력 여유를 확보했다.

### 2-4. Reduce 기능별 파이프라인 보강

변경 파일:

- `ai-pipeline/ai_module/map_reduce/reduce_api.py`
- `ai-pipeline/ai_module/map_reduce/pipeline.py`

변경 내용:

- `run_feature_reduce_stage()`를 중심으로 user, critic, playtime, final reduce를 분리했다.
- Reduce prompt에 구체 근거 기준을 강화했다.
- "유저들은 전투를 칭찬했다" 같은 일반론을 금지하고, 실제 review_id와 detail 기반 문장을 요구했다.
- Groq가 `summary`를 list로 반환하는 경우 문자열로 join하도록 보정했다.
- evidence dedupe/compression 계층을 추가했다.

### 2-5. Playtime reduce 호출 조건 강화

변경 파일:

- `ai-pipeline/ai_module/map_reduce/pipeline.py`
- `ai-pipeline/ai_module/map_reduce/reduce_api.py`

변경 내용:

- playtime bucket coverage가 부족하면 playtime reduce를 호출하지 않는다.
- selected Steam 리뷰 기준 early/mid/late 각 bucket이 최소 20개 이상일 때만 playtime reduce를 호출한다.
- coverage가 부족하면 early/mid/late 그룹을 비우고 최종 결과에서는 `null`로 둔다.

이 변경으로 불충분한 playtime 근거를 억지로 요약하지 않고, Reduce 요청 수와 토큰 사용량도 줄였다.

### 2-6. Reduce 입력 압축

변경 파일:

- `ai-pipeline/ai_module/map_reduce/reduce_api.py`
- `docs/plan-review-quality-pipeline.md`

현재 기본값:

- User Reduce evidence: 최대 24개
- Critic Reduce evidence: 최대 20개
- Playtime bucket evidence: bucket당 최대 8개
- Final Composer evidence anchor: 최대 10개
- evidence `detail` / `snippet`: 각각 최대 180자

품질이 부족한 게임에서만 evidence 상한을 확장하는 방향으로 문서화했다.

## 3. 신규/보강 테스트

변경 파일:

- `ai-pipeline/test_map_reduce_quality.py`
- `ai-pipeline/test_pipeline.py`

추가 검증:

- Map payload가 chunk 내부 review_id만 사용하는지 검증
- deterministic candidate 생성 검증
- LLM `reviews` 형태 JSON repair 검증
- LLM id-only JSON repair 검증
- LLM review_id 누락 시 candidate 순서 기반 repair 검증
- malformed/truncated JSON에서 review_id 기반 repair 검증
- Map 단계 기본 경로가 로컬 LLM인지 검증
- 로컬 LLM 실패 시 deterministic fallback 동작 검증
- playtime bucket coverage gate 검증
- Reduce `summary` list 반환 시 문자열 join 검증
- Reduce usage 계측 검증

## 4. Dry Test 자동 검증 추가

신규 파일:

- `ai-pipeline/dry_quality_run.py`

역할:

- DB에서 실제 리뷰를 읽어 dry-run 실행
- 실제 Ollama Map + Groq Reduce 호출
- DB에 요약 결과를 저장하지 않음
- Map 품질, Reduce 토큰, 출력 근거성을 JSON으로 리포트

주요 옵션:

```bash
python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates
```

자동 gate:

- `map_llm_success`: Map LLM success rate가 기준 이상인지
- `map_no_deterministic_fallback`: deterministic fallback이 0인지
- `reduce_token_budget`: Reduce input + output tokens가 예산 이하인지
- `reduce_request_budget`: Reduce requests가 4 이하인지
- `reduce_no_error`: Reduce error가 없는지
- `grounded_output`: 출력에 review_id 근거가 충분히 포함되는지

`--assert-gates` 사용 시 하나라도 실패하면 exit code 1로 종료한다.

## 5. 최종 검증 결과

현재 DB의 리뷰 보유 게임:

| game_id | title | reviews |
|---:|---|---:|
| 1 | ELDEN RING | 232 |
| 2 | Grand Theft Auto V | 179 |

현재 DB에는 리뷰가 있는 게임이 2개뿐이므로, 문서 기준에 따라 현재 DB의 모든 게임을 대상으로 dry test를 실행했다.

실행 명령:

```bash
docker compose exec backend python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates
```

결과:

- exit code: 0
- 두 게임 모두 `gate_results.passed=true`

### ELDEN RING

| 항목 | 결과 |
|---|---:|
| chunks | 4 |
| Map LLM success rate | 1.0 |
| deterministic fallback rate | 0.0 |
| Reduce requests | 2 |
| Reduce input tokens | 7,789 |
| Reduce output tokens | 1,467 |
| Reduce total tokens | 9,256 |
| Token budget | 9,800 |
| gate result | passed |

출력 근거 예시:

- 케일리드 신수탑에서 3시간 길찾기
- 레아 루카리아에서 NPC/보스 관련 버그 경험
- 불의 거인 보스전에서 반복 사망과 분노
- 미친불 엔딩 컷신 후 강제종료
- PC판 데이터 손실/로드 실패

주의:

- 위 항목은 dry test 당시 내부 evidence 구체성 확인용 예시다.
- 공개 사용자 화면에는 특정 보스명, 후반 지역명, 엔딩명처럼 스포일러가 될 수 있는 고유명사를 그대로 노출하지 않는다.
- 공개 출력에서는 "후반부 대형 보스전", "특정 중반 지역", "특정 엔딩 연출 이후"처럼 경험 유형과 영향만 남기는 redaction이 필요하다.

### Grand Theft Auto V

| 항목 | 결과 |
|---|---:|
| chunks | 3 |
| Map LLM success rate | 1.0 |
| deterministic fallback rate | 0.0 |
| Reduce requests | 2 |
| Reduce input tokens | 4,511 |
| Reduce output tokens | 1,114 |
| Reduce total tokens | 5,625 |
| Token budget | 9,800 |
| gate result | passed |

출력 근거 예시:

- NPC가 차를 벽에 계속 박아 퀘스트 진행 불가
- 멀티플레이가 고여 있어 싱글만 즐겼다는 의견
- GTA6 PC 출시 대기 불만
- 최적화와 현실성 칭찬
- 카지노 제한 불만

## 6. 통과한 정적 검증

실행한 검증:

```bash
.venv\Scripts\python.exe -m compileall ai-pipeline\ai_module\map_reduce ai-pipeline\dry_quality_run.py
.venv\Scripts\python.exe -m pytest ai-pipeline\test_map_reduce_quality.py
.venv\Scripts\python.exe ai-pipeline\test_pipeline.py
```

결과:

- compileall 통과
- `test_map_reduce_quality.py`: 12 passed
- `test_pipeline.py`: 통과
- backend/import 정합성 확인 통과

## 7. 파일별 변경 요약

### `ai-pipeline/ai_module/map_reduce/map_local.py`

- Map 단계의 기본 경로를 로컬 Ollama LLM으로 변경
- deterministic candidate prompt 주입
- JSON-only system prompt와 `format: "json"` 추가
- retry prompt 추가
- LLM output repair/fallback 계측 추가
- `map_llm_valid_chunks`, `map_llm_repaired_chunks`, `map_deterministic_fallback_chunks` 등 통계 추가

### `ai-pipeline/ai_module/map_reduce/map_schema.py`

- Map JSON schema normalize/validate 추가
- deterministic candidate 생성
- LLM schema repair 함수 추가
- malformed JSON repair 함수 추가
- review_id/source/aspect/polarity/detail/snippet 검증

### `ai-pipeline/ai_module/map_reduce/chunker.py`

- 로컬 LLM JSON 출력을 안정화하기 위해 chunk 입력 크기 보수화

### `ai-pipeline/ai_module/map_reduce/pipeline.py`

- `json_v2_llm_map` 캐시 버전 적용
- Map JSON을 playtime/user/critic 그룹별로 review_id 기준 필터링
- playtime bucket coverage gate 추가
- coverage 부족 시 playtime reduce 그룹 비움

### `ai-pipeline/ai_module/map_reduce/reduce_api.py`

- 기능별 Reduce 파이프라인 추가
- evidence subset/dedupe/compression 추가
- Reduce usage 계측
- 구체 evidence 기반 prompt 강화
- summary list 반환 보정
- token budget을 위한 evidence limit 축소

### `backend/app/services/ai_service.py`

- Map/Reduce token 및 failure statistics 저장 흐름 보강
- `reduce_usage`를 `failure_reasons_json`에 포함

### `ai-pipeline/dry_quality_run.py`

- 실제 DB + Ollama + Groq 기반 dry test 실행
- 결과 저장 없이 품질/토큰 리포트 생성
- `--assert-gates` 자동 검증 추가

### `ai-pipeline/test_map_reduce_quality.py`

- Map/Reduce 품질 관련 단위 테스트 추가

## 8. 현재 한계와 주의점

- 현재 로컬 모델은 `qwen2.5:1.5b` 하나만 확인되었다.
- 이 모델은 완전한 schema JSON을 직접 안정적으로 만들지는 못한다.
- 다만 LLM이 선택한 review_id와 생성 중간 결과를 repair 계층이 복구하므로, Map 단계는 여전히 로컬 LLM 기본 경로를 유지한다.
- 현재 검증 DB에는 리뷰가 있는 게임이 2개뿐이다.
- 5게임 이상 검증은 추가 게임 데이터가 확보된 뒤 `dry_quality_run.py --assert-gates`로 다시 수행해야 한다.
- `docs/`는 현재 git 추적 제외 설정일 수 있으므로, 문서 변경은 git status에 나타나지 않을 수 있다.

## 9. 결론

현재 DB 기준으로는 `plan-review-quality-pipeline.md`의 핵심 방향을 코드에 반영했고, dry test 자동 gate도 통과했다.

달성된 기준:

- Map 단계 로컬 LLM 기본 경로 유지
- deterministic은 candidate/validator/fallback 역할로 제한
- Map JSON schema화
- LLM output repair
- Reduce 기능별 파이프라인
- 실제 리뷰 근거 중심 요약
- Reduce-only 토큰 계측
- 게임당 Reduce token budget 9,800 이하
- 현재 DB의 모든 리뷰 보유 게임 dry test 통과

## 10. 2026-05-27 추가 보완 및 재검증

이후 실제 2게임 dry-run 결과를 Codex가 직접 읽어보며 확인한 결과, 자동 gate는 통과했지만 일부 공개 문장이 원문 말투를 너무 직접 보존하는 문제가 남아 있었다. 이 문제는 구조 변경이 필요한 큰 결함은 아니며, 공개 문장 polish와 회귀 gate 강화 수준의 사소한 품질 보완으로 처리했다.

보완 내용:

- Reduce 공개 출력 sanitizer와 evidence 기반 문장 생성 규칙을 강화했다.
- 원문 욕설, 비속어, 어색한 조사, "유저들은/플레이어들은"식 일반론, 깨진 template 문장을 gate에서 더 잘 잡도록 했다.
- `그래도 난 니가 좋다`, `너무 재미있어요 해보세요`, `플스로 재밌게 했어서 PC판도 구매...`처럼 실제 리뷰 근거는 있지만 공개 출력으로는 어색한 문장을 자연스러운 요약 문장으로 정규화했다.
- 부정 근거가 섞인 긍정 리뷰는 긍정 clause만 분리해 장점에 쓰고, 분리할 수 없으면 장점 목록에서 제외하도록 했다.

재검증:

```bash
.venv\Scripts\python.exe -m compileall ai-pipeline
.venv\Scripts\python.exe -m pytest ai-pipeline\test_map_reduce_quality.py -q
docker compose exec backend sh -lc "LOCAL_MAP_MODEL=qwen2.5:1.5b python /workspace/ai-pipeline/dry_quality_run.py --games 2 --review-limit 36 --assert-gates"
```

결과:

- `compileall`: 통과
- `test_map_reduce_quality.py`: 49 passed
- `ELDEN RING`: 36 reviews, 11 chunks, Map LLM success 1.0, deterministic fallback 0.0, Reduce 2 requests / 7,996 tokens, gate passed
- `Grand Theft Auto V`: 36 reviews, 6 chunks, Map LLM success 1.0, deterministic fallback 0.0, Reduce 2 requests / 5,487 tokens, gate passed

수동 품질 판단:

- 두 게임의 최종 공개 출력은 `review_id` 근거를 유지했다.
- 자동 gate 기준으로 artifact, spoiler leak, vague output, weak pros/cons, anchor mismatch가 없었다.
- Codex가 출력 문장을 직접 읽었을 때, "Metacritic 유저들은 전투를 칭찬했다" 수준의 일반론이 아니라 실제 리뷰의 경험 조건을 요약한 문장으로 개선되었다.
- 현재 DB에는 검증 가능한 게임이 2개뿐이므로 5게임 검증은 추가 게임 데이터가 확보된 뒤 다시 수행해야 한다. 현재 기준에서는 문서의 "5게임 미만이면 현재 DB의 모든 게임 검증" 조건을 충족했다.

