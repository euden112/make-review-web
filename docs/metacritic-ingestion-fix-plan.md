# Metacritic 데이터 적재 공백 원인 및 수정 계획

작성일: 2026-05-21

## 1. 현상

상세 화면에서 Metacritic 대표 리뷰와 비평가 리뷰 요약이 비어 있다.

현재 DB 확인 결과:

| 항목 | 값 |
| --- | ---: |
| `platforms`의 Metacritic row | 존재 |
| `external_reviews` Steam 리뷰 | 755건 |
| `external_reviews` Metacritic 리뷰 | 0건 |
| 최근 Metacritic `ingestion_runs` | `success`, `fetched_count=0`, `inserted_count=0` |

즉, 프론트 렌더링 문제가 아니라 Metacritic 리뷰가 수집/적재되지 않은 상태다.

## 2. 직접 원인

### 2-1. Metacritic DOM 변경으로 크롤러 selector가 깨짐

현재 크롤러는 리뷰 카드를 다음 selector로 찾는다.

```python
cards = await page.query_selector_all("div.review-card__content")
```

실제 Metacritic 페이지 확인 결과:

| Selector | 결과 |
| --- | ---: |
| `div.review-card__content` | 0 |
| `.review-card__content` | 0 |
| `.review-card` | 0 |
| `[data-testid*=review]` | 51 |

현재 DOM은 `data-testid` 기반 구조다.

```html
<div data-testid="review-card">
  <div data-testid="review-card-content">
    <div data-testid="review-card-date">Aug 29, 2022</div>
    <a data-testid="review-card-header">...</a>
    <div data-testid="review-card-quote-block">...</div>
  </div>
</div>
```

따라서 페이지 접근은 성공하지만 리뷰 카드를 0개로 인식하고, 크롤러는 정상 종료처럼 0건 raw 파일을 생성한다.

### 2-2. 0건 수집이 실패로 취급되지 않음

`crawling/metacritic/metacritic_crawler.py`는 critic/user 수집 결과가 모두 0건이어도 exit code 0으로 종료한다.

그 결과 `demo.py`는 Metacritic 크롤링 성공으로 판단하고 `send_to_api.py metacritic`을 계속 실행한다.

### 2-3. backend ingestion도 0건 payload를 success로 기록

`backend/app/api/v1/reviews.py`의 `/api/v1/reviews/metacritic`은 `game_data.reviews`가 빈 배열이어도 다음처럼 처리된다.

- `Game` upsert
- `GamePlatformMap` upsert
- `IngestionRun(status="started")` 생성
- 리뷰 upsert는 생략
- `run.status = "success"`
- `fetched_count = 0`
- `inserted_count = 0`

이 때문에 운영 로그에는 실패가 아니라 성공으로 남는다.

### 2-4. 파일명 정책이 혼재되어 있음

`crawling/send_to_api.py`에는 다음 설정이 있다.

```python
"metacritic": {
    "input_file": "metacritic/reviews_metacritic.json",
}
```

하지만 실제 전송 파일 탐색은 이 값을 사용하지 않고 다음 패턴만 찾는다.

```python
search_pattern = str(BASE_DIR / platform / "*_reviews_raw_*.json")
```

현재 오래된 `crawling/metacritic/reviews_metacritic.json`은 전송 대상이 아니다.

## 3. 수정 방향

### 3-1. 크롤러 selector를 `data-testid` 기반으로 교체

대상 파일:

- `crawling/metacritic/metacritic_crawler.py`

변경 방향:

```python
cards = await page.query_selector_all('[data-testid="review-card"]')
```

필드 추출도 현재 DOM 기준으로 교체한다.

| 필드 | 기존 selector | 신규 selector |
| --- | --- | --- |
| 카드 | `div.review-card__content` | `[data-testid="review-card"]` |
| 날짜 | `.review-card__date` | `[data-testid="review-card-date"]` |
| 헤더/작성자 | `.review-card__header` | `[data-testid="review-card-header"]` |
| 본문 | `.review-card__quote` | `[data-testid="review-card-quote-block"]` |
| 점수 | `.c-siteReviewScore span` | `.c-siteReviewScore span` 유지 가능 |

작성자는 header 전체 텍스트에서 점수 숫자를 제거해 정제한다.

### 3-2. Read More 처리 fallback 정리

현재 DOM에서 본문은 기본적으로 quote block에 노출된다.

우선순위:

1. `[data-testid="review-card-quote-block"]`에서 본문 추출
2. `Read More` 버튼이 있고 본문이 잘린 경우 modal 또는 링크 이동 처리
3. 실패 시 해당 카드 skip

초기 수정에서는 1번을 우선 구현하고, full review 링크 추적은 후속 개선으로 둔다.

### 3-3. 0건 수집을 실패로 처리

크롤러 전체 결과에서 모든 게임의 `record_count` 합이 0이면:

- stderr 또는 명확한 stdout 메시지 출력
- exit code 1 반환
- 0건 raw 파일은 생성하지 않거나, 생성하더라도 `send_to_api.py`가 전송하지 못하게 한다.

권장 정책:

```python
total_records = sum(data["meta"]["record_count"] for data in all_output.values())
if total_records == 0:
    raise SystemExit(1)
```

단, 일부 게임만 0건인 경우는 partial success로 허용할 수 있다. 이 경우 게임별 경고를 출력한다.

### 3-4. `send_to_api.py`에서 0건 payload 전송 차단

대상 파일:

- `crawling/send_to_api.py`

변경 방향:

- `*_reviews_raw_*.json`에서 최신 파일을 읽은 뒤 전체 리뷰 수를 계산한다.
- 전체 리뷰 수가 0이면 전송하지 않고 exit code 1 반환.
- 성공한 전송만 원본 파일 삭제.

예상 검증:

```text
[metacritic] 전송 중단: 리뷰가 0건입니다.
```

### 3-5. backend ingestion에서 0건 Metacritic payload를 success로 기록하지 않기

대상 파일:

- `backend/app/api/v1/reviews.py`

정책 후보:

| 안 | 동작 | 장점 | 단점 |
| --- | --- | --- | --- |
| A | 0건 게임은 `failed` ingestion run 기록 후 422 반환 | 실패가 명확함 | 일부 게임만 0건일 때 전체 요청 실패 가능 |
| B | 0건 게임은 `partial` 또는 `failed` run 기록, 나머지 게임은 처리 | 운영 로그가 정확함 | 구현이 조금 복잡함 |

권장: B.

게임별 `reviews`가 비어 있으면:

- `IngestionRun.status = "failed"` 또는 `"partial"`
- `fetched_count = 0`
- `inserted_count = 0`
- `error_message`가 없으므로 현재 스키마에 없으면 `ingestion_dead_letters` 사용 또는 로그 warning

현재 스키마에 `error_message` 컬럼이 없으므로, 최소 구현은 다음으로 충분하다.

- 빈 리뷰 게임은 `run.status = "failed"`
- HTTP 응답에 `empty_games` 배열 포함
- 모든 게임이 비었으면 422 반환

### 3-6. `demo.py` 검증 강화

대상 파일:

- `demo.py`

추가 검증:

- `send_to_api("metacritic")` 이후 DB에서 Metacritic 리뷰 수 확인
- `--skip-metacritic`이 아닌 경우 Metacritic 리뷰 수가 0이면 실패 처리
- `critic_summaries`가 0건인 경우 원인이 “critic 리뷰 부족”인지 “Metacritic 적재 없음”인지 구분 출력

검증 SQL:

```sql
select count(*)
from external_reviews r
join platforms p on p.id = r.platform_id
where p.code = 'metacritic';
```

## 4. 기대 결과

수정 후 정상 상태:

| 영역 | 기대 결과 |
| --- | --- |
| Metacritic crawler | 실제 critic/user 리뷰를 1건 이상 수집 |
| send_to_api | 0건 raw 파일 전송 차단 |
| backend ingestion | 0건 payload를 success로 오기록하지 않음 |
| DB | `external_reviews`에 platform=`metacritic` 리뷰 존재 |
| AI summary | `metacritic_critic_avg`, `metacritic_user_avg` 계산 가능 |
| frontend | Metacritic 대표 리뷰/비평가 요약이 데이터 존재 시 렌더 |

## 5. 검증 계획

### 단독 크롤러 검증

```powershell
cd crawling
python metacritic/metacritic_crawler.py --games elden-ring
```

합격 기준:

- exit code 0
- raw 파일 생성
- `critic_count + user_count > 0`

### 전송 검증

```powershell
cd crawling
python send_to_api.py metacritic
```

합격 기준:

- HTTP 200 또는 201
- 응답에 저장 성공 표시
- DB의 Metacritic 리뷰 수 증가

### DB 검증

```sql
select g.normalized_title, p.name, count(r.id)
from games g
join external_reviews r on r.game_id = g.id
join platforms p on p.id = r.platform_id
group by g.normalized_title, p.name
order by g.normalized_title, p.name;
```

합격 기준:

- 대상 게임별 `Metacritic` row가 1건 이상 존재

### E2E 검증

```powershell
python demo.py --test --scenario all --skip-docker --force --timeout 900 --verify-frontend
```

합격 기준:

- Metacritic 적재 검증 PASS
- 대표 리뷰 섹션에 Metacritic 데이터가 존재하는 경우 표시
- 비평가 리뷰 데이터가 충분한 경우 critic summary 생성
- Metacritic 데이터가 부족한 경우에도 이유가 명확히 표시

## 6. 우선순위

| 우선순위 | 작업 |
| --- | --- |
| P0 | Metacritic selector 교체 |
| P0 | 크롤러 0건 수집 실패 처리 |
| P0 | `send_to_api.py` 0건 payload 전송 차단 |
| P1 | backend ingestion 0건 success 오기록 방지 |
| P1 | `demo.py` Metacritic DB 적재 검증 추가 |
| P2 | 오래된 `reviews_metacritic.json` 정책 정리 |
| P2 | Read More/full review 추적 보강 |

## 7. 반영 결과

반영일: 2026-05-21

| 항목 | 상태 | 근거 |
| --- | --- | --- |
| Metacritic selector 교체 | 완료 | `data-testid="review-card"` 기반 수집으로 변경, 기존 selector는 fallback으로만 유지 |
| 크롤러 0건 실패 처리 | 완료 | 전체 `record_count` 합이 0이면 exit code 1 |
| `send_to_api.py` 0건 payload 차단 | 완료 | 최신 raw 파일의 리뷰 수가 0이면 전송 전 exit code 1 |
| backend ingestion 0건 success 오기록 방지 | 완료 | 전체 0건 payload는 upsert 전 HTTP 422, 정제 후 0건인 게임은 ingestion run `failed` 기록 |
| `demo.py` Metacritic DB 적재 검증 | 완료 | Metacritic 전송 후 대상 게임의 DB 적재 수가 0이면 테스트 실패 |
| 오래된 파일명 정책 정리 | 완료 | `send_to_api.py`의 미사용 `reviews_metacritic.json` 설정 제거, `*_reviews_raw_*.json`만 전송 기준으로 사용 |
| Read More fallback 정리 | 완료 | quote block 우선 추출, modal 본문은 보강 fallback으로 처리 |
| 프론트 상세 검증 | 완료 | `--verify-frontend`에서 Metacritic 대표 리뷰/비평가 요약/추천 대상 섹션 렌더 확인 |

검증 결과:

- 단독 크롤러: `elden-ring` 기준 전문가 61건, 유저 50건 수집.
- 전체 데모: `python demo.py --test --scenario all --reset-volumes --force --timeout 900 --verify-frontend` exit code 0.
- DB 적재: `grand-theft-auto-v` Metacritic 99건, `elden-ring` Metacritic 60건.
