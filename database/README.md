# Sprint 1 Database Plan and Implementation

이 폴더는 Sprint 1 담당 범위인 정제 데이터 DB 적재(Insert/Upsert) 모듈 구현을 위한 DB 설계/SQL을 포함합니다.

## Database ERD

리뷰 수집, 적재, 정규화, AI 요약 흐름을 정리한 ERD입니다.
핵심 테이블은 `external_reviews`이며, 이 테이블을 기준으로 게임/플랫폼 매핑, 수집 실행 이력, 언어별 요약 잡, 요약 결과가 연결됩니다.
중복 방지 키(`source_review_key`), DB 트리거 기반 점수 정규화, 게임/언어별 증분 요약 커서 구조까지 함께 확인할 수 있습니다.

<img src="database_architecture.drawio.svg" alt="Database ERD" />

## 범위

- 외부 리뷰 수집 데이터 저장 (Steam, Metacritic)
- 게임 마스터/플랫폼 매핑
- 배치 실행 이력 저장
- 중복 방지 + Upsert
- 플랫폼별 점수 체계 정규화
- AI 증분 요약(map-reduce) 운영 메타데이터 저장
- 언어별 요약 결과 관리

## 파일

- 01_schema.sql: 테이블/제약/기본 데이터
- 02_upsert_templates.sql: 적재 모듈에서 바로 사용할 Upsert SQL 템플릿
- 03_migration_sprint2.sql: 기존 DB를 Sprint 2 스펙(감성 분석/토큰/캐시 지표/샘플링 인덱스)으로 확장하는 additive 마이그레이션

## 적용 순서

1. PostgreSQL DB 생성
2. 01_schema.sql 실행
3. 적재 코드에서 02_upsert_templates.sql의 쿼리 사용

## 기존 DB 업그레이드 순서 (데이터 유지)

1. 기존 DB 백업
2. 03_migration_sprint2.sql 실행
3. 애플리케이션이 신규 컬럼에 쓰기 로직을 점진 반영

참고:
- `create table if not exists`는 기존 테이블의 컬럼을 자동으로 변경하지 않으므로, 이미 운영 중인 DB는 03 마이그레이션 적용이 필요합니다.

## 설계 핵심

1. 데이터 흐름
- 플랫폼 메타 수집: `game_platform_map.platform_meta_json`
- 리뷰 적재: `external_reviews`에 원문, 점수, 작성자, 메타데이터 저장
- 점수 정규화: `fn_normalize_review_score()` 트리거가 `normalized_score_100` 계산
- 운영 추적: `ingestion_runs`가 수집 시작/종료/건수를 기록
- AI 요약: `game_summary_cursor` → `review_summary_jobs` → `review_summary_chunks` → `game_review_summaries` 순으로 처리하며, `language_code`로 언어별 결과를 분리 관리

1. 중복 방지
- 리뷰 원천 고유키가 있는 경우: source_review_id 사용
- 없는 경우: source_review_key(작성자+날짜+본문 해시 등) 사용
- DB 유니크 키: (platform_id, game_id, source_review_key)
- `is_deleted`는 소프트 삭제 플래그로, 재적재 시 같은 리뷰를 복구하는 데 사용

2. Upsert 기준
- ON CONFLICT (platform_id, game_id, source_review_key)
- 리뷰 본문/점수/도움수/플레이타임/수정시각 갱신
- `source_review_id`가 없더라도 `source_review_key`로 동일 리뷰를 식별 가능

3. 점수 체계 정규화 (확장 가능)
- Steam: 추천/비추천 -> binary(100/0)
- Metacritic critic: 100점 체계
- Metacritic user: 10점 체계(공통 100점 축으로 변환)
- DB 트리거가 normalized_score_100을 자동 계산
- `score_raw`가 숫자가 없는 값(N/A, 별점없음 등)일 때도 트랜잭션이 깨지지 않도록 방어 로직 적용
- **신규 플랫폼 추가 시: score_scales, review_types 테이블에 행만 추가하면 됨**

4. 확장성 개선 (Phase 5)
- `score_scales`: 플랫폼별 점수 체계 관리 (binary, 10, 100, 5 등)
- `review_types`: 리뷰 타입 관리 (user, critic, tomatometer 등)
- external_reviews: review_type/score_scale이 이제 FK(ID) 참조
- **이제 새 플랫폼/타입 추가 시 테이블 DDL 수정 불필요, 참조 데이터만 추가**
- `platform_meta_json`, `source_meta_json`, `pros_json`, `cons_json`, `keywords_json`는 jsonb로 저장해 플랫폼별/요약별 유연한 확장을 지원

5. AI 요약 구조 
- game_summary_cursor: 마지막 요약 반영 review_id 기록
- review_summary_jobs: 요약 배치 작업 상태 추적 (시작/종료, 처리 범위, 청크 개수)
- review_summary_chunks: map 단계 청크별 요약 저장
- game_review_summaries: 최종 요약 버전 관리 (is_current)
- `game_review_summaries`는 현재 노출 중인 버전을 `is_current = true`로 관리하고, 게임/언어별 현재본 1건만 유지하도록 설계

6. Sprint 1 완료 검증
- 동일 데이터 재적재 시 중복 insert가 발생하지 않아야 함
- Steam + Metacritic 각 1개 게임 조회 시 리뷰가 정상 조회되어야 함
- `idx_external_reviews_*` 인덱스는 최신 리뷰 조회, 플랫폼 필터, delta 스캔, 운영 로그 조회를 빠르게 하기 위한 보조 구조

## 프론트 노출 예시

- Steam 최신 n개 리뷰
- Metacritic critic 최신 n개 리뷰
- Metacritic user 최신 n개 리뷰
- AI 요약 리뷰(현재 버전 1건)