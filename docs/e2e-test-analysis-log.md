# 로컬 E2E 테스트 — 최종 분석 로그 (2026-05-18)

브랜치 `feature/review-restructure` · 스펙 `docs/test-scenario-plan.md`
러너 `demo.py --test` (어서션 기반, PASS/FAIL 집계 + exit code)

---

## 1. 최종 결과

| 실행 | 시나리오 | 결과 | exit |
|---|---|---|---|
| regression | `--scenario regression` | **5/5 PASS** | 0 |
| 전체 E2E (4차, 유효 키) | `--scenario all` | **41/41 PASS** | 0 |

TS-1 파이프라인 · TS-2 기능 A(buy-signal) · TS-3 기능 C(highlights) ·
TS-4 기능 D(divergence) · TS-5 폐지·회귀 · TS-6 신선도 전부 PASS.

---

## 2. 수정 내역 (분리 커밋)

| 커밋 | 파일 | 근본원인 | 분류 |
|---|---|---|---|
| `f67f469` | docker-compose.yml | 삭제된 `09_migration_sprint5_events.sql` 마운트 잔존 → postgres initdb 실패. 실존 파일(`09_sprint4_fixes`, `sprint6_fixes`)로 정합화 | 설정 결함 |
| `f67f469` | backend/app/core/redis_client.py | `ai_service`가 import하는 `invalidate_playtime_cache`·`invalidate_critic_cache` 미정의 → ImportError로 backend 부팅 실패 | 코드 결함 |
| `cadc55a` | demo.py | (1) R-2 자기검사가 어서션 문자열 리터럴과 자기충돌 → AST 기반 import/함수정의 검사로 교정 (2) Windows cp949 ✓ 출력 크래시 → stdout/stderr UTF-8 고정 (3) regression 단독 시 빈 DB에서 STEP7 abort → 계속 진행 | 테스트 어서션 오류 |
| `19f1818` | backend/app/models/domain.py | `summaries.py`가 `joinedload(GameReviewSummary.job)`·`summary.job` 사용하나 모델에 `job` 관계 누락 → `/summary` 500 → STEP9 타임아웃 | 코드 결함 |
| (파일삭제) | backend/.../events.cpython-311.pyc | 폐지 소스의 stale 컴파일 캐시 (BUG-7) | 크러프트 |

> 기능 로직(plan-purchase-desire-features.md 스펙)은 테스트 통과 목적으로
> 변경하지 않음. 수정은 실제 결함·테스트 어서션 오류·크러프트에 국한.

---

## 3. 실패 → 해결 추적 (은폐 없이 전 과정)

### 단계 0 — 환경 차단
- Docker 데몬 미기동 → Docker Desktop 기동 후 진행
- `docker compose up` 실패: 삭제된 events.sql 마운트 → **수정 `f67f469`**
- backend 부팅 실패: redis_client 함수 누락 → **수정 `f67f469`**

### 단계 1 — regression
- 1차: `R-2`(자기충돌 어서션 오류)·`R-4`(stale `events.pyc`) FAIL
- 조치: R-2 AST 검사로 교정, pyc 삭제 → **재실행 5/5 PASS** (`cadc55a`)

### 단계 2 — 전체 E2E (4회 실행, 근본원인 분리)

| 회차 | 증상 | 근본원인 | 판별 |
|---|---|---|---|
| 1차 | TS-1 pros/cons/aspect FAIL, GTA STEP9 타임아웃 | (a) `GameReviewSummary.job` 누락 500 (b) **GROQ_API_KEY 만료** `401 expired_api_key` | 코드 결함 1건 수정(`19f1818`) / 키 만료는 환경 — 우회 없이 보고 |
| 2차 | 동일 FAIL | `docker restart`가 변경된 `.env` 미반영 (컨테이너 구 키 잔존, 끝4자 `jt76`) | 환경 — `compose up --force-recreate` 필요 |
| 3차 | 동일 FAIL | 컨테이너는 신규 키(`OM3T`)지만, 만료-키 시절 Redis에 적재된 **stale 오류-요약**(TTL 24h)을 STEP9 폴링이 수신 | DB·API 원본 정상 확증: psql `pros_json` 정상 한글, httpx `bool(pros)=True`. 콘솔 `�`는 cp949 표시 artifact |
| 4차 | **41/41 PASS** | stale 캐시 `game_summary:*` 삭제 후 `--force` 재실행 | 해결 |

핵심 판별: "유니코드 깨짐"으로 보였던 현상은 **터미널 cp949 표시 artifact**였고
(API `encoding=utf-8`, JSON 정상 파싱, `bool=True`), 실제 데이터는 손상 없음.
TS-1 FAIL의 진짜 원인은 ① 만료 키 ② docker restart의 env 미반영
③ stale Redis 오류-캐시 — 모두 환경 요인이며 코드 결함이 아님.

---

## 4. 재현 절차

```bash
# 0. 전제: Docker Desktop 기동, .env에 유효한 GROQ_API_KEY
docker compose up -d --force-recreate backend scheduler   # .env 키 주입

# 1. 저비용 회귀 (크롤·요약 불필요)
python demo.py --test --scenario regression --skip-docker --skip-crawl
#   → 5/5 PASS, exit 0

# 2. 전체 E2E (크롤은 1회 후 재사용 가능)
docker exec capstone_redis redis-cli FLUSHDB           # stale 캐시 제거
python demo.py --test --scenario all --skip-docker --skip-crawl --force --timeout 900
#   → 41/41 PASS, exit 0
```

> 주의: `.env` 키 변경 시 `docker restart`로는 반영 안 됨 →
> `docker compose up -d --force-recreate` 필수. 키 교체·만료 후에는
> `game_summary:*` Redis 캐시(TTL 24h)에 오류-요약이 남을 수 있어 FLUSHDB 권장.

---

## 5. 결론

- 테스트 러너(demo.py) 및 기능 A·C·D·파이프라인·폐지 정리는 **스펙 충족**.
- 발견된 코드/설정 결함 3건은 모두 수정·검증 완료.
- 외부 의존(Groq)·캐시·컨테이너 env 전파는 환경 요인으로 정확히 분리,
  환경 조치(키 주입·캐시 클리어)로 해결.
- 최종 상태: `--scenario all` **41/41 PASS, exit 0** 재현 가능.
