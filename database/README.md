# Sprint 1 Database Plan and Implementation

이 폴더는 Sprint 1 담당 범위인 정제 데이터 DB 적재(Insert/Upsert) 모듈 구현을 위한 DB 설계/SQL을 포함합니다.

## 범위

- 외부 리뷰 수집 데이터 저장 (Steam, Metacritic)
- 게임 마스터/플랫폼 매핑
- 배치 실행 이력 저장
- 중복 방지 + Upsert

## 파일

- 01_schema.sql: 테이블/제약/기본 데이터
- 02_upsert_templates.sql: 적재 모듈에서 바로 사용할 Upsert SQL 템플릿

## 적용 순서

1. PostgreSQL DB 생성
2. 01_schema.sql 실행
3. 적재 코드에서 02_upsert_templates.sql의 쿼리 사용

## 설계 핵심

1. 중복 방지
- 리뷰 원천 고유키가 있는 경우: source_review_id 사용
- 없는 경우: source_review_key(작성자+날짜+본문 해시 등) 사용
- DB 유니크 키: (platform_id, game_id, source_review_key)

2. Upsert 기준
- ON CONFLICT (platform_id, game_id, source_review_key)
- 리뷰 본문/점수/도움수/플레이타임/수정시각 갱신

3. Sprint 1 완료 검증
- 동일 데이터 재적재 시 중복 insert가 발생하지 않아야 함
- Steam + Metacritic 각 1개 게임 조회 시 리뷰가 정상 조회되어야 함
