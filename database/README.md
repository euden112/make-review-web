# Database Plan and Implementation

이 폴더는 PostgreSQL 스키마, 초기 데이터, 마이그레이션 SQL, ERD를 포함합니다. 현재 스키마는 리뷰 적재뿐 아니라 AI 요약의 커서, 실행 로그, 통합/유저/평론가/플레이타임 결과 저장까지 담당합니다.

## Database ERD

리뷰 수집, 적재, 정규화, AI 요약 흐름을 정리한 ERD입니다.

<img src="database_architecture.drawio.svg" alt="Database ERD" />

## 범위

- 외부 리뷰 수집 데이터 저장(Steam, Metacritic)
- 게임 마스터/플랫폼 매핑
- 수집 실행 이력 저장
- 중복 방지 + Upsert
- 플랫폼별 점수 체계 정규화
- AI 증분 요약 커서와 실행 로그
- 통합 요약 메타데이터, 유저 상세 요약, 평론가 요약, 플레이타임 구간별 요약 저장
- 추천 대상, 주의 대상, aspect 점수 JSON 저장

## 파일

- `01_schema.sql`: 기본 테이블/제약/초기 데이터
- `02_upsert_templates.sql`: 적재 모듈에서 사용할 Upsert SQL 템플릿
- `03_migration_sprint2.sql`: 감성 분석/토큰/캐시 지표/샘플링 인덱스 확장
- `04_migration_sprint3_m001.sql` ~ `07_migration_sprint3_m005.sql`: Sprint 3 확장
- `08_migration_sprint4.sql`: `playtime_analyses`, `critic_summaries` 추가
- `09_migration_sprint4_fixes.sql`: Sprint 4 타입/제약 보정
- `10_migration_sprint4_failure_stats.sql`: 요약 실패 통계
- `10_migration_sprint6_fixes.sql`: Sprint 6 보정. Docker 초기화 시 `11_migration_sprint6_fixes.sql` 이름으로 마운트됩니다.
- `12_migration_one_liner_separation.sql`: 한줄평(`one_liner`) 분리
- `13_migration_user_summary_split.sql`: `user_summaries` 추가, `game_review_summaries.summary_text` nullable 처리
- `14_migration_recommendation_targets.sql`: `recommended_for_json`, `caution_for_json` 추가

## 적용 방식

새 DB는 루트의 `docker-compose.yml`이 `database/*.sql`을 `/docker-entrypoint-initdb.d/`에 마운트해 파일 이름 순서대로 초기 적용합니다.

```bash
docker compose up -d postgres
```

이미 운영 중인 DB에는 SQL 파일을 직접 순서대로 적용해야 합니다. `CREATE TABLE IF NOT EXISTS`는 기존 테이블의 컬럼을 자동 변경하지 않으므로, 데이터 유지 업그레이드에서는 반드시 마이그레이션 파일을 실행합니다.

## 설계 핵심

### 리뷰 적재

- 핵심 테이블은 `external_reviews`입니다.
- 중복 방지는 `(platform_id, game_id, source_review_key)` 유니크 키로 처리합니다.
- 원천 고유 ID가 있으면 `source_review_id`를 사용하고, 없으면 작성자/날짜/본문 해시 기반 `source_review_key`를 사용합니다.
- Steam, Metacritic 점수는 `normalized_score_100`으로 정규화됩니다.
- `score_scales`, `review_types`를 참조 데이터로 둬 신규 플랫폼/리뷰 타입 추가 시 DDL 변경을 줄입니다.

### AI 요약

- `game_summary_cursor`: 게임별 마지막 요약 반영 `external_reviews.id`를 기록합니다. 증분 요약의 경계이므로 DB 이관 시 리뷰 ID 연속성이 중요합니다.
- `review_summary_jobs`: 요약 배치 실행 상태와 처리 범위, 토큰/실패 통계를 기록합니다.
- `review_summary_chunks`: Map 단계 청크별 요약 저장용 테이블입니다.
- `game_review_summaries`: 현재 노출되는 통합 요약 메타데이터입니다. `is_current = true` 행이 현재본입니다.
- `user_summaries`: 유저 리뷰 상세 본문과 장단점을 저장합니다.
- `critic_summaries`: Metacritic 평론가 리뷰 요약을 저장합니다.
- `playtime_analyses`: 초반/중반/후반 구간별 요약, 점수, 리뷰 수를 저장합니다.

`game_review_summaries.summary_text`는 마이그레이션 13 이후 null일 수 있습니다. 현재 상세 본문은 `user_summaries.summary`, `critic_summaries.summary`, `playtime_analyses`에 분리 저장하고, 통합 테이블은 한줄평, 추천/주의 대상, aspect 점수 JSON 같은 카드 메타데이터를 담당합니다.

### Aspect 점수 JSON

`game_review_summaries.aspect_sentiment_json`에는 aspect별 다음 값이 저장됩니다.

```json
{
  "content": {
    "label": "콘텐츠/볼륨",
    "score": 6.8,
    "baseline_score": 5.9,
    "evidence_count": 12,
    "evidence_review_ids": [1, 2, 3]
  }
}
```

현재 허용 aspect는 `graphics`, `controls`, `optimization`, `content`, `story`, `price_value`, `sound`, `gameplay`, `difficulty`입니다. `content`는 콘텐츠 양과 할 거리, `story`는 서사·캐릭터·세계관을 뜻합니다.

## 프론트 노출 예시

- 게임 목록과 장르 필터
- 통합 한줄평과 카테고리 레이더
- 유저 상세 요약
- 평론가 요약
- 플레이타임 구간별 요약
- 추천 대상/주의 대상
- 구매 타이밍 시그널
