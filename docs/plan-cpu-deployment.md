# 학교 클라우드 CPU 배포 계획

## 배경

학교 클라우드는 GPU가 없어 Ollama 로컬 LLM이 CPU로 동작해야 한다. 단, 로컬 LLM + 외부 API 하이브리드 아키텍처는 캡스톤 기술 기여 요소이므로 유지한다.

현재는 로컬 GPU 환경에서 테스트 중이므로 보류 상태이며, 학교 클라우드 배포 직전에 아래 순서로 적용한다.

---

## 현재 환경 vs 클라우드 환경 비교

| 항목 | 로컬 (GPU) | 학교 클라우드 (CPU) |
|------|-----------|---------------------|
| Map 모델 | `gemma3:4b` | `gemma3:1b` 권장 |
| 추론 속도 | 5~15 tok/s (GPU) | 20~35 tok/s (CPU + 1B) |
| RAM 사용 | ~3.5GB | ~0.9GB |
| 50게임 배치 | ~분~시간 단위 | ~5시간 (1B 기준) |

---

## 적용 순서

### 1. Map 모델 교체: `gemma3:4b` → `gemma3:1b`

환경변수 `LOCAL_MAP_MODEL`을 변경한다.

```yaml
# docker-compose.yml
environment:
  - LOCAL_MAP_MODEL=gemma3:1b
```

**근거**: Map 단계는 반구조화 추출(PROS/CONS/ASPECTS/IDS) 작업이므로 대형 모델이 필요하지 않다. 1B 모델로도 충분히 포맷을 준수한다.

### 2. Ollama 파라미터 최적화

[map_local.py](../ai-pipeline/ai_module/map_reduce/map_local.py)의 `summarize_chunk_with_ollama` 옵션 수정:

```python
"options": {
    "temperature": 0.2,
    "num_predict": 400,    # 2048 → 400 (Map 출력 실제 150~200 토큰)
    "num_ctx": 2048,       # 4096 → 2048 (청크 입력 ~1,400 토큰)
    "num_thread": 0,       # 추가 (물리 코어 수 자동 감지)
}
```

- `num_predict` 축소: 불필요한 생성 루프 제거
- `num_ctx` 축소: KV 캐시 메모리 절반 감소 → prefill 속도 향상
- `num_thread=0`: Ollama가 호스트 물리 코어 수를 자동 감지하도록 위임

### 3. Docker Compose GPU 예약 제거

[docker-compose.yml](../docker-compose.yml)의 nvidia GPU reservation 블록을 제거하거나 별도 compose 파일로 분리한다.

```yaml
# 현재 — CPU 호스트에서 컨테이너 시작 실패 원인
deploy:
  resources:
    reservations:
      devices:
        - driver: nvidia
          count: 1
          capabilities: [gpu]
```

이 블록이 남아 있으면 NVIDIA Container Toolkit이 없는 CPU 호스트에서 **Ollama 컨테이너 자체가 시작 실패**한다.

권장 방식: `docker-compose.cpu.yml`로 분리하여 환경별 compose 파일을 운영한다.

---

## 운영 고려사항

- Map 단계는 사용자 요청에 실시간 응답하는 것이 아니라 **백그라운드 배치 작업**이다.
- gemma3:1b + CPU 최적화 기준 초기 50게임 적재는 약 5시간 (야간 배치 가능).
- 이후 증분 처리는 게임당 ~6분이며, Map 캐시 덕분에 재처리 대상은 신규 청크만 해당된다.

---

## 검증 항목

배포 직전 확인:

- [ ] `LOCAL_MAP_MODEL` 환경변수가 `gemma3:1b`로 설정되어 있는가
- [ ] `num_predict`, `num_ctx`, `num_thread` 변경 적용되었는가
- [ ] CPU 호스트에서 `docker-compose up`이 GPU 오류 없이 동작하는가
- [ ] 단일 게임 적재 시 Map 단계가 timeout(300초) 안에 완료되는가
- [ ] Reduce(Groq API) 호출이 정상 동작하는가 (외부 API라 환경 영향 없음)
