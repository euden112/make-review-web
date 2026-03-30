# Sprint 1 Database Plan and Implementation

이 폴더는 Sprint 1 담당 범위인 정제 데이터 DB 적재(Insert/Upsert) 모듈 구현을 위한 DB 설계/SQL을 포함합니다.

## 범위

- 외부 리뷰 수집 데이터 저장 (Steam, Metacritic)
- 게임 마스터/플랫폼 매핑
- 배치 실행 이력 저장
- 중복 방지 + Upsert
- 플랫폼별 점수 체계 정규화
- AI 증분 요약(map-reduce) 운영 메타데이터 저장

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

3. 점수 체계 정규화 (확장 가능)
- Steam: 추천/비추천 -> binary(100/0)
- Metacritic critic: 100점 체계
- Metacritic user: 10점 체계(공통 100점 축으로 변환)
- DB 트리거가 normalized_score_100을 자동 계산
- **신규 플랫폼 추가 시: score_scales, review_types 테이블에 행만 추가하면 됨**

4. 확장성 개선 (Phase 5)
- `score_scales`: 플랫폼별 점수 체계 관리 (binary, 10, 100, 5 등)
- `review_types`: 리뷰 타입 관리 (user, critic, tomatometer 등)
- external_reviews: review_type/score_scale이 이제 FK(ID) 참조
- **이제 새 플랫폼/타입 추가 시 테이블 DDL 수정 불필요, 참조 데이터만 추가**

5. AI 요약 구조 
- game_summary_cursor: 마지막 요약 반영 review_id 기록
- review_summary_jobs: 요약 배치 작업 상태 추적 (시작/종료, 처리 범위, 청크 개수)
- review_summary_chunks: map 단계 청크별 요약 저장
- game_review_summaries: 최종 요약 버전 관리 (is_current)

6. Sprint 1 완료 검증
- 동일 데이터 재적재 시 중복 insert가 발생하지 않아야 함
- Steam + Metacritic 각 1개 게임 조회 시 리뷰가 정상 조회되어야 함

## 프론트 노출 예시

- Steam 최신 n개 리뷰
- Metacritic critic 최신 n개 리뷰
- Metacritic user 최신 n개 리뷰
- AI 요약 리뷰(현재 버전 1건)