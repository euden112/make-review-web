# Query Assets (AI Pipeline Scope)

이 디렉토리는 AI 파이프라인 담당 범위에서 사용하는 순수 쿼리 애셋을 보관합니다.

## 파일

- upsert_external_reviews.sql
  - 사용처: 수집/정제 결과를 external_reviews에 대량 Upsert할 때 사용
  - 바인딩 방식: $1 자리에 jsonb payload를 전달 (jsonb_to_recordset)

- sampling_queries.py
  - 사용처: Map-Reduce 입력 샘플링 전 비율 계산
  - STEAM_RATIO_QUERY: 긍/부정 비율 계산
  - METACRITIC_BIN_RATIO_QUERY: 점수 구간(low/mid/high) 비율 계산

## 주의

- 현재는 backend 관할 분리를 위해 backend/app/queries에 두지 않고 ai-pipeline에 둡니다.
- 추후 백엔드 팀이 실제 서비스에 통합할 때 소유권에 맞게 위치를 재조정할 수 있습니다.
