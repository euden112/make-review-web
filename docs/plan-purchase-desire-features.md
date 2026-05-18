# 리뷰 구조 변경 및 구매 욕구 유발 기능 기획서 (A·C·D)

> 작업 브랜치: **`feature/review-restructure`** (베이스 `6a97e24`). 메인 테마: 리뷰 구조 변경(기능 D) + 구매 욕구 유발(기능 A·C).

## 1. 배경

서비스 목표 계층을 재정의한다.

```
1차: 구매 결정 지원   (소비자 — 살지 판단)        ← 기존 기능 충실
2차: 구매 욕구 유발   (소비자 — 사고 싶게)        ← 공백, 본 기획 대상
최종: 게임사/플랫폼 협업 (B2B 수익)
```

기존 구현 기능은 대부분 1차(판단 도구)에 집중되어 있고, 2차(욕구 유발) 전용 기능은 사실상 공백이다. 이슈 트래킹 기능은 폐지하되, 그 인프라(`histogram_crawler`, `news_crawler`)를 욕구 유발 방향으로 **프레이밍을 반전**하여 재활용한다.

### 기능 분담

| 기능 | 담당 | 본 문서 |
|---|---|---|
| A. 구매 타이밍 시그널 | backend (이슈 트래킹 자산 전환) | ✅ |
| B. 취향 기반 발견 피드 | AI 챗봇 개발 파트 | ❌ (위임) |
| C. 감성 하이라이트 | backend + AI 파이프라인 | ✅ |

---

## 2. 설계 원칙: 결정 지원 ≠ 욕구 유발

| | 결정 지원 (기존) | 욕구 유발 (본 기획) |
|---|---|---|
| 정보 성격 | 중립 (안 살 수도 있음) | 긍정 편향·열망 자극 |
| 트리거 | 사용자가 게임 지정 | 시스템이 먼저 제안 |
| 심리 레버 | 판단 근거 | FOMO·타이밍·감정 이입 |

---

## 3. 기능 A — 구매 타이밍 시그널

### 3-1. 개념

할인 진행 + 최근 여론 긍정 피크가 겹치는 시점에 *"평가도 최고, 할인까지 — 지금이 적기"* 신호를 노출한다. 이슈 트래킹이 `negative_spike`로 **경고**했다면, 본 기능은 `positive_recovery + sale`로 구매를 **점화**한다. 판정 코드 골격은 동일, 조건만 반전.

### 3-2. 데이터 소스

| 소스 | 상태 | 용도 |
|---|---|---|
| `crawling/steam/histogram_crawler.py` | 재사용 (이슈 트래킹에서 이관) | 여론 긍정 회복/역대 최고 구간 판정 |
| `crawling/steam/news_crawler.py` (sale 분류) | 재사용 | 세일 이벤트 발생 여부 (약한 신호) |
| Steam `appdetails` API | **신규 필요** | 실제 할인율·정가·세일 종료일 |

> `news_crawler`의 sale 분류는 "세일 관련 공지가 올라왔다"는 약한 프록시일 뿐이다. 실제 할인율(`price_overview.discount_percent`)과 종료일은 `https://store.steampowered.com/api/appdetails?appids={appid}` 호출로 보강한다 (인증 불필요).

### 3-3. 판정 로직

```
buy_signal(game) =
    할인 중 (discount_percent > 0)
    AND (
        여론 긍정 회복 (histogram direction == positive_recovery)
        OR 현재 부정 비율이 역대 최저 구간 (neg_ratio ≈ 최소값)
    )
→ 신호 강도: 할인율 + 여론 개선폭으로 산출
```

### 3-4. API

```
GET /api/v1/games/{game_id}/buy-signal

Response:
{
  "is_good_timing": true,
  "discount_percent": 50,
  "original_price": 39000,
  "final_price": 19500,
  "sentiment_state": "positive_recovery",
  "price_as_of": "2026-05-18T17:30:00Z",   // 가격 스냅샷 기준 시각
  "reasons": ["50% 할인 중", "최근 평가가 역대 최고 구간"]
}
```

> **BUG-3 결정 (스펙 축소, 권장안 채택)**: Steam `appdetails`가 세일 종료일을 미제공 → `sale_ends_at`·카운트다운 제거. FOMO 레버는 **할인율**로 유지. 대신 `price_as_of`(가격 스냅샷 시각)를 노출해 준실시간임을 명시.

목록 페이지 일괄 조회 부담을 줄이기 위해 게임 목록 응답에 경량 플래그(`is_good_timing`, `discount_percent`)만 포함하고, 상세 사유는 본 엔드포인트로 분리한다.

### 3-5. UI

- `GameListPage` 카드: "지금이 적기" 배지 + 할인율
- `GameDetailPage` 히어로: 사유 + 할인율 강조 + "가격은 Steam에서 확인" 스토어 링크 (카운트다운 없음)

### 3-5b. 데이터 신선도 설계 (가격 staleness·레이트리밋·크롤링 비용 대응)

> **설계 정정 (2026-05-18)**: 초기 "가격 전용 잡 1~3h 주기"는 과설계로 폐기. Steam 할인은 거의 전부 **17:00 UTC 경계에서 일 1회 토글**(시즈널 세일·데일리/미드위크 딜·퍼블리셔 딜 모두 Steamworks 스케줄러가 이 경계 사용)되므로, 시간 단위 폴링은 정보 가치 없이 비용만 12배. **일 1회 17:05 UTC 정렬 패스**로 변경하고, 멀티 appid 배치로 호출량을 추가 압축한다.

`buy-signal`은 변동 속도가 다른 두 데이터로 구성된다.

| 구성 | 변동성 | 갱신 |
|---|---|---|
| 가격·할인 (appdetails) | **일 1회(≈17:00 UTC) 토글** | 일 1회 17:05 UTC 정렬 패스, **멀티 appid 배치** |
| 여론·histogram | 주~월 완만 | 같은 일일 패스에 포함 (appid별, 배치 불가) |

**핵심 원칙: 사용자 요청은 Steam을 직접 호출하지 않고 Redis 캐시만 읽는다.**

1. **일일 정렬 리프레셔**: 매일 17:05 UTC(가격 경계 직후 + 전파 버퍼)에 1회 패스. 가격은 `appdetails?filters=price_overview`에 **콤마 다중 appid**로 배치 조회(청크 실패 시 해당 청크만 단건 폴백), 여론은 appid별 조회. 결과를 Redis에 `game_id`별 저장. 1차 실패분만 2차 패스에서 재시도. 429 시 백오프 + 마지막 성공값 유지(graceful degrade)
2. **신선도 게이팅**: 가격 스냅샷이 신선도 임계(`PRICE_STALE_SECONDS` ≈ 28h = 일 1패스 + 여유) 초과면 `is_good_timing`을 강제 `false`로 degrade — 확신 없는 할인을 단정하지 않음
3. **UX 헤지**: `price_as_of` 표기 + 스토어 링크로 최종 확인 위임

**효과**: ① staleness ≤ 28h(가격이 어차피 일 1회만 변동하므로 정보 손실 없음) ② 외부 호출량 = 일 단위 상수, 사용자 트래픽과 독립 → 레이트리밋 노출 0. 100게임 기준: 가격 ~5req(배치) + 여론 ~100req = **일 ~105req**(한도의 1% 미만, 기존 2h 루프 ~1,200req 대비 ~12배, 배치 포함 시 가격만 ~20배 절감)

> 여론을 주 1회로 더 줄이는 안은 폐기됨: 가격 배치 후 Steam 예산이 충분(한도 1%↓)하고, 일일 단일 패스로 통일하면 운영이 단순하며, 긍정 회복 점화(buy-signal 핵심 레버)의 신선도 이점이 크다. Groq 일 한도는 AI 요약 파이프라인에만 적용되며 여론 경로(순수 histogram 크롤·계산)와 무관.

### 3-6. 이슈 트래킹 자산 전환

| 이슈 트래킹 (폐지) | 구매 타이밍 시그널 (신규) |
|---|---|
| `histogram_crawler` → 변곡점 경고 | → 긍정 피크 점화 |
| `news_crawler` → 논란/패치 매칭 | → 세일 이벤트 감지 |
| `event_service` 오케스트레이션 | → signal 산출로 재배치 |
| `GameEvent` 테이블 | 미사용 (드롭 또는 보류) |

폐지를 손실이 아닌 **자산 전환**으로 만든다 — 매몰비용 회수 + 출구 전략.

### 3-6. 이슈 트래킹 자산 전환

| 이슈 트래킹 (폐지) | 구매 타이밍 시그널 (신규) |
|---|---|
| `histogram_crawler` → 변곡점 경고 | → 긍정 피크 점화 |
| `news_crawler` → 논란/패치 매칭 | → 세일 이벤트 감지 |
| `event_service` 오케스트레이션 | → signal 산출로 재배치 |
| `GameEvent` 테이블 | 미사용 (드롭 또는 보류) |

폐지를 손실이 아닌 **자산 전환**으로 만든다 — 매몰비용 회수 + 출구 전략.

---

## 4. 기능 C — 감성 하이라이트

### 4-1. 개념

대표 리뷰를 *신뢰 근거(중립)* 가 아닌 **감정 피크(긍정 편향)** 기준으로 재선별 노출한다. *"엔딩에서 울었다", "200시간이 순삭"* 류의 강렬한 원문으로 감정 이입을 유도한다.

| 기존 대표 리뷰 | 감성 하이라이트 |
|---|---|
| 속성 등급 근거 (중립) | 감정 피크 (긍정 편향) |
| 신뢰성 확보 목적 | 욕구 점화 목적 |

### 4-2. 데이터 소스 — 신규 수집 불필요

`ExternalReview`에 필요한 필드가 모두 존재한다.

| 필드 | 용도 |
|---|---|
| `review_text_clean` | 원문 노출 |
| `helpful_count` | 공감도 가중 |
| `playtime_hours` | 몰입 근거 ("200시간") |
| `is_recommended` / `normalized_score_100` | 긍정 필터 |
| `review_categories_json` | 강점 속성 연결 |

### 4-3. 선별 알고리즘

```
highlight_score(review) =
    긍정 (is_recommended == true OR score 상위)
    × helpful_count 가중
    × 감정 강도 (감정 키워드 "인생/최고/소름/울었/순삭/중독" 포함,
                 느낌표·길이 등 표현 강도)
→ 상위 N개 선별, 속성 평가(aspect_scores) 상위 항목과 매핑
```

### 4-4. API

```
GET /api/v1/games/{game_id}/highlights

Response:
{
  "highlights": [
    {
      "review_id": 482,
      "text": "100시간 넘게 했는데 아직도 질리지 않는다. 인생 게임.",
      "playtime_hours": 112,
      "helpful_count": 340,
      "linked_aspect": "content"
    }
  ]
}
```

기존 `representative_reviews` 경로를 공유하되 선별 기준만 분기. AI 파이프라인 재실행 없이 수집 리뷰에서 직접 산출 가능.

### 4-5. UI

- `GameDetailPage`: "이 게임의 명장면" 캐러셀 섹션
- 속성 평가와 페어링 — "그래픽 9.2 → 왜 그런지 보기" 로 강점 증폭

---

## 5. 구현 순서

> ⚠️ **본 절은 초기 계획이며 폐기됨.** 기능 A·C·프론트·이슈 트래킹 폐지는 모두 완료(7-2 참조). 유효한 잔여 작업 순서는 **9-3**으로 일원화됨.

(초기 계획 — 이력 보존용)

| 순위 | 작업 | 상태 |
|---|---|---|
| 1 | C. 감성 하이라이트 | ✅ 완료 |
| 2 | A. `appdetails` 수집 | ✅ 완료 |
| 3 | A. 판정 로직 + signal API | ✅ 완료 |
| 4 | A·C 프론트엔드 | ✅ 완료 |
| 5 | 이슈 트래킹 폐지 정리 | ✅ 완료(백엔드)/⚠️ 잔존(BUG-4·9) |

---

## 6. 폐지·전환 요약

- 이슈 트래킹 폐지로 `histogram_crawler`·`news_crawler`는 버려지지 않고 기능 A로 이관
- 기능 C는 기존 `ExternalReview`·`aspect_scores` 자산을 재선별만으로 욕구 유발 자산화
- 두 기능 모두 신규 대규모 수집 없이 2차(구매 욕구 유발) 공백을 메움
- B(발견 피드)는 AI 챗봇 파트가 별도 담당하여 2차 목표를 입구·상세·추천 3면에서 커버

---

## 7. 브랜치 재정비 및 작업 로드맵

### 7-1. 배경 및 기준점

이슈 트래킹 폐지와 방향 전환에 따라 실제 공유 베이스 **`6a97e24`** 에서 다시 시작했다. (초기 `7c592a4`로 잡았다 검증 후 정정)

```
6a97e24 "fix(sprint4): DB 타입·하드코딩·마이그레이션 수정"   ← 확정 베이스
  = origin/feature/playtime-critic-user-analysis 의 tip
  ├── feature/review-restructure  ← 현재 작업 브랜치 (구 feature/purchase-desire, rename됨)
  ├── feature/ai-chatbot
  └── feature/cloud-deploy
```

- `6a97e24`는 Sprint4 DB 정합성 수정 포함: `playtime_analyses`/`critic_summaries` FLOAT→NUMERIC·INTEGER→BIGINT, API `tags`/`rating` 필드 추가 — **기능 C·D와 직접 관련**, 현재 브랜치 베이스에 포함됨
- `feature/issue-tracking-test`는 유효 작업 이관 완료 후 **로컬·원격 모두 삭제됨** (이력은 `backup/issue-tracking-test-archive`에 보존)

### 7-2. 완료된 작업 (코드 검증 기준 2026-05-17)

작업 브랜치 **`feature/review-restructure`** (베이스 `6a97e24`). rebase 및 후속 정리로 커밋이 재구성되어, 아래는 **현재 브랜치 기준 커밋**.

| 항목 | 상태 | 현재 브랜치 커밋 |
|---|---|---|
| AI 파이프라인 CPU 최적화 이관 | ✅ 완료 | `2a76178` |
| 번역 API·게임 비교 이관 | ✅ 완료 | `456ac9f`, `55351b6`, `translate.py` |
| 이슈 트래킹 폐지 | ⚠️ 백엔드 완료/잔존 | `bbad28c` (데모 재편) — BUG-4·9 잔존 |
| 기능 C — 감성 하이라이트 (API) | ✅ 완료 | `ce56dcb`, `highlights.py` 라우팅됨 |
| 기능 A — appdetails 수집 | ✅ 완료 | `1f3c785`, `appdetails_crawler.py` |
| 기능 A — buy-signal API | ✅ 완료 | `24709f8`, `buy_signal.py` 라우팅됨 |
| A·C 프론트엔드 | ✅ 완료 | `f2c924e` (배지+명장면 캐러셀) |
| A·C·AI 통합 정리 | ✅ 완료 | `ab42b03` |

> 정정 이력: 7-2의 구 커밋 해시(`930ea5f`·`a66d787` 등)는 rebase 전 것으로 현재 브랜치에 없음. 위 표는 `git log 6a97e24..feature/review-restructure` 기준 실제 해시.

### 7-3. 기준점 정렬 — 완료

| 항목 | 상태 | 비고 |
|---|---|---|
| 구 `feature/purchase-desire` → `6a97e24` rebase | ✅ 완료 | 17커밋 재적용(`6a97e24..HEAD`), 충돌 3파일 해결 |
| 충돌 해결: `summaries.py` | ✅ | `6a97e24` tags/rating(Metacritic 환산) + 배치 쿼리 최적화 **병합** (검증: 구문·로직 정상) |
| 충돌 해결: `GameListPage.jsx` | ✅ | API `g.tags`(GAME_META 제거) + 대소문자 무시 검색 + buy-signal 배지 병합 |
| 충돌 해결: `GameDetailPage.jsx` | ✅ | API `game.rating` 사용(제거된 `meta.rating` 폐기) + buy-signal 배너 보존 |
| 브랜치 rename | ✅ | `feature/purchase-desire` → `feature/review-restructure` (로컬·원격, 구 원격 브랜치 삭제) |
| 원격 반영 | ✅ | `origin/feature/review-restructure` push 완료 (upstream 추적) |
| 백업 ref (롤백 안전망) | ✅ | `backup/purchase-desire-prerebase` (rebase 전), `backup/purchase-desire-remote-archive` (구 원격 `ab42b03`), `backup/issue-tracking-test-archive` (폐지 브랜치 `8e6bfcb`) |

### 7-4. 남은 작업

> 본 절의 작업은 **9-3으로 일원화**됨 (버그 수정 + 기능 D + 검증 + git 정렬). 9-3 참조.

### 7-5. 자산 이관 주의사항

- 자산 이관(AI 최적화·번역·게임 비교)·기능 A·C·기준점 rebase·브랜치 rename·원격 push는 **완료** (7-2, 7-3)
- 이슈 트래킹 폐지는 백엔드 완료, **프론트(`sentiment-trend`)·`demo.py` 잔존**(BUG-4·9) — 9-3 #2에서 완결
- `game_events` 타입 버그는 이슈 트래킹 폐지로 자연 해소됨 (완료)
- rebase 충돌 해결 시 `6a97e24` 의도(GAME_META 제거→API tags/rating) 우선 적용 — dead `meta.rating` 폐기됨. 프론트 렌더 검증 필요(9-3 #10)

---

## 8. 기능 D — 유저/평론 분리 요약 (결정 지원 강화)

> **분류 주의**: 본 기능은 2차(욕구 유발)가 아닌 **1차(구매 결정 지원) 강화** 기능이다. 기능 A·C와 별개 트랙으로 다룬다. 2차 갭은 메우지 않으나, 비대칭 프레이밍(8-4)으로 일부 2차에 기여한다.

### 8-1. 개념

현재: 통합 요약(한줄평+pros/cons+속성+full_text) + 비평가 요약 별도 제공.

변경: **통합 요약은 짧은 한줄평만**, 본문은 **유저 리뷰 요약 / 평론가 리뷰 요약 2개 트랙으로 분리·동격 제공**.

기대 효과: "라스트 오브 어스 2"(평론 93 / 유저 5점대)처럼 **유저-평론 괴리를 사용자가 명확히 인식** → 혼합 점수의 오도 방지. 기획서 페르소나 2(*"평론가 점수보다 실제 유저들의 가감 없는 의견"*) 직격.

### 8-2. 현재 구현 대비 변경점

| 구성 | 현재 | 변경 후 |
|---|---|---|
| 통합 요약 | 한줄평+pros/cons+속성+full_text | **한줄평만 (괴리 인지형)** |
| 유저 요약 | "unified"에 혼재 | **독립 트랙 분리** |
| 평론가 요약 | `analysis.py:critic-summary` 별도 존재 | 동격 트랙으로 승격 |
| 괴리 표시 | 없음 (의도적 금지) | **괴리 지표 신규** |

> `reduce_api.py`의 critic은 이미 BucketSummary로 분리되어 있어 신규 파이프라인이 아닌 **재배치** 중심. 단, 현재 critic 규칙 *"never compare or mention divergence"* 를 반전해야 함.

### 8-3. 평가 요약

| 항목 | 판정 |
|---|---|
| 1차 결정 지원 | ⭐ 매우 강함 (페르소나 2 명시 니즈) |
| 2차 욕구 유발 | ◐ 양방향 (8-4 비대칭 프레이밍으로 일부 전환) |
| 구현 비용 | 낮음 (critic 이미 분리, 재배치 중심) |
| 리스크 | 인지 부하 증가, 괴리 없는 게임엔 잉여, 의도된 설계 반전 |

### 8-4. 핵심 설계: 동적·비대칭 노출

1. **한줄평은 항상 괴리 인지형**: 예) *"평론 호평, 유저 혹평 — 호불호 분명"*
2. **2트랙 강조는 괴리 임계 초과 시에만** (대부분 게임은 유저≈평론 → 잉여 방지)
3. **유저↑평론↓ → "숨은 명작" 프레이밍 → 욕구 점화(2차 전환)**. 평론↑유저↓ → 구매 주의 신호. 이 비대칭이 1차 기능을 부분적으로 2차에 기여시키는 핵심
4. **톤 가드**: AI가 편향적 단정을 하지 않도록 괴리 서술을 사실 기반(점수 차·표본)으로 제한

### 8-5. 추가 여부 결론

**조건부 추가 채택.** 저비용·고재사용이며 기획서 명시 니즈를 직격. 단 (a) 1차 기능으로 정직히 분류, (b) 8-4 동적·비대칭 노출을 필수 조건으로 구현. 작업 우선순위는 기능 C 이후, 기능 A와 병행 가능(파이프라인 영역이 달라 충돌 적음).

---

## 9. 정적 분석 결과 및 수정 작업 (2026-05-17)

전체 코드 정적 분석 결과. Python 구문(`compileall`)은 전체 정상. 아래는 발견된 버그·오류·미완 항목.

### 9-1. 진행 현황 (2026-05-17 BUG 수정·기능 D 완료 후 갱신)

| 기능 | 상태 | 비고 |
|---|---|---|
| 기능 A — appdetails 수집 (`appdetails_crawler.py`) | ✅ 완료 | BUG-1·2 수정, 크롤러 연결됨 |
| 기능 A — buy-signal API (`buy_signal.py`) | ✅ 완료 | 스펙축소·Redis read-only·신선도 게이팅 구현. 리프레셔→스케줄러→compose `scheduler` 잡 컨테이너로 운영화 (A안 실패 격리 적용) |
| 기능 C — 감성 하이라이트 (`highlights.py`) | ✅ 완료 | BUG-5·6·8 수정 (정렬·영어키워드·캐싱) |
| A·C 프론트엔드 (wiring) | ✅ 정상 | vite build 통과 (28 모듈) |
| 이슈 트래킹 폐지 | ✅ 완료 | BUG-4·9 완결 (프론트 sentiment-trend·demo 잔존 제거) |
| 기능 D | ✅ 완료 | `/divergence` API + 괴리 지표 패널 (8-4 동적·비대칭) |

### 9-2. 버그 처리 결과

정적 분석 9건 + **E2E 테스트 중 추가 발견 2건(BUG-10·11)** = 총 11건, **전부 해결**. (BUG-3: 스펙축소 + 리프레셔/스케줄러 운영화 + compose 잡 컨테이너 완료. BUG-10·11: 정적 분석이 import·런타임 경로를 못 본 한계로 누락됐던 실결함, `docs/e2e-test-analysis-log.md`에서 발견·수정)

| ID | 심각도 | 요약 | 상태 | 커밋 |
|---|---|---|---|---|
| BUG-1 | 높음 | Steam 가격 minor-unit(×100) 미환산 → 가격 100배 표시 | ✅ 해결 | `682742e` |
| BUG-2 | 중간 | `appdetails`·`histogram` 크롤러 인라인 재구현(데드코드·중복) | ✅ 해결 | `deda870` |
| BUG-3 | 중간 | `sale_ends_at` 항상 None → 세일 카운트다운 동작 불가 | ✅ 해결 (스펙축소 + 리프레셔·스케줄러·compose 운영화) | 3-4·3-5b·9-3 |
| BUG-4 | 중간 | 프론트가 폐지된 `/sentiment-trend` 호출·렌더 | ✅ 해결 | `91118a9` |
| BUG-5 | 낮음 | `highlights` `.limit(3000)` 정렬 없음 → 명장면 누락 가능 | ✅ 해결 | `ad89270` |
| BUG-6 | 낮음 | 감정 키워드 한국어 전용 → 영어 리뷰 편향 | ✅ 해결 | `ad89270` |
| BUG-7 | 낮음 | 폐지 `events.pyc` 추적 잔존 | ✅ 해결 | `844be5d` |
| BUG-8 | 낮음 | buy-signal/highlights 캐싱 없음 | ✅ 해결 | `cf6b2ba` |
| BUG-9 | 중간 | `demo.py` 이슈 트래킹 흐름 잔존(폐지 상태 불일치) | ✅ 해결 | `91118a9` |
| BUG-10 | **높음** | `ai_service`가 import하는 `invalidate_playtime_cache`·`invalidate_critic_cache` 미정의 → ImportError로 backend 부팅 차단 (정적 분석 누락, E2E서 발견) | ✅ 해결 | `f67f469` |
| BUG-11 | 중간 | `summaries.py`가 `joinedload(GameReviewSummary.job)` 사용하나 모델에 `job` 관계 누락 → `/summary` 500 (정적 분석 누락, E2E서 발견) | ✅ 해결 | `19f1818` |

> 정상 확인: `GameEvent`/`EventSummary` 잔존 참조 없음. rebase 병합 `summaries.py:get_games` 구문·로직 정상.
> **검증 한계 명시**: 표의 ✅는 코드 수정 실재를 git에서 검증한 것. `--scenario all` **41/41 PASS는 런타임 주장**으로, Docker·유효 GROQ 키·크롤 동반 재실행으로만 확정됨 (재현 절차: `e2e-test-analysis-log.md` §4).
> **정적 분석 한계 교훈**: BUG-10·11은 구문/단일파일 검사로는 안 잡히는 import·ORM 관계 결함. 향후 검토 시 `python -c "import app.main"` 류 부팅 스모크를 정적 단계에 포함 권장.

### 9-3. 작업 현황 — 완료 요약 + 잔여

**완료 (10/11 항목)** — 모두 `feature/review-restructure` 브랜치에 반영됨.

| 작업 | 커밋 |
|---|---|
| BUG-1 가격 100배 수정 | `682742e` |
| BUG-2 크롤러 통합 | `deda870` |
| BUG-4·9 이슈 트래킹 폐지 완결 | `91118a9` |
| BUG-5·6 highlights 편향 보정 | `ad89270` |
| BUG-7 `__pycache__` 정리 | `844be5d` |
| BUG-8 Redis 캐싱 | `cf6b2ba` |
| 기능 D — 괴리 지표 API | `8368816` |
| 기능 D — 괴리 지표 프론트 | `098e6df` |
| 기능 C·프론트 검증 | compileall·vite build·라우터 통과 |
| `main`·`playtime`·`origin/main` 정렬 | `e0d13dd` (force-push 승인 완료) |

**리프레셔 운영화 + 스케줄링 — ✅ 전부 완료 (2026-05-18)**

(진단 이력 보존) 코드 검증 결과: 스펙 축소(`sale_ends_at` 제거·`price_as_of` 추가), buy-signal/list Redis read-only, 신선도 게이팅, `price_refresher.py` 모듈 자체는 구현 완료였으나, **리프레셔를 기동하는 스케줄러가 없어** Redis 스냅샷이 비어 buy-signal이 항상 `is_good_timing=false`로만 응답(기능 A 운영상 비활성)하던 상태였다. "1~3h 주기"는 과설계로 정정됨(3-5b). → **해소 완료**: `ai_batch.py` 추출 + `scheduler.run_daily` 실패 격리(A안) + compose `scheduler` 잡 컨테이너 추가로 운영화. 항목별 상태는 아래 표(전부 ✅):

| 잔여 항목 | 내용 | 상태 |
|---|---|---|
| 멀티 appid 배치 | `appdetails_crawler.py` `fetch_price_info_batch` — `filters=price_overview` 콤마 다중 appid(20개/청크) + 청크 실패 시 단건 폴백 | ✅ 코드 완료 |
| 리프레셔 일일 정렬 | `price_refresher.py` 재작성 — 매일 17:05 UTC 정렬, 가격 배치, 1차 실패분만 2차 재시도, 여론 같은 패스 일단위 통일 | ✅ 코드 완료 |
| 스케줄러 잡 | `app/jobs/scheduler.py` 신규 — 17:05 UTC까지 sleep → 가격·여론 리프레셔 → AI 요약 증분 배치 직렬화. `--once`/`--loop` | ✅ 코드 완료 |
| 신선도 임계 상향 | `buy_signal_logic.PRICE_STALE_SECONDS` `5h` → `28h` (일 1패스 + 여유) | ✅ 코드 완료 |
| 스펙 축소·read-only | `sale_ends_at` 제거·`price_as_of`·신선도 게이팅·Redis read-only | ✅ 코드 완료 |
| 스케줄러 작업 분리 (A안) | `_ai_summary_batch`를 `app/jobs/ai_batch.py`(독립 `--once/--loop`)로 추출 + `scheduler.run_daily`에서 리프레셔·AI를 각각 `try`로 감싸 실패 독립화 | ✅ 코드 완료 (`ai_batch.py` 신규, scheduler 리팩터) |
| compose `scheduler` 서비스 | `docker-compose.yml`에 격리 잡 컨테이너(`python -m app.jobs.scheduler --loop`, `restart: unless-stopped`) 추가 | ✅ 반영 (compose config 검증 통과) |

> **스케줄러 메커니즘 결정 (2026-05-18)**: 잡 컨테이너 방식 채택. 근거 — ① AI 요약 배치(CPU 추론 수십 분)를 API 프로세스와 격리해야 응답 지연 없음 ② `uvicorn --reload`에 APScheduler를 얹으면 잡 중복 실행(레이트리밋 폭증·Groq 한도 소진) ③ `price_refresher.py`에 `--loop` 골격 존재, compose가 이미 오케스트레이션. cron 사이드카는 차선(컨테이너 비용 동일 + crontab·시간대 관리 표면 추가). 가격·AI 두 잡을 17:05 UTC 한 타임라인에 배치.

> **스케줄러 작업 분리 결정 (2026-05-18, A안)**: 가격·여론 리프레셔와 AI 요약 배치는 **데이터 의존이 없다**(AI는 크롤된 리뷰만 의존, Redis 가격 스냅샷 미참조 / buy-signal은 Redis만, divergence는 저장 요약만 참조 → 순서 불요). 따라서 직렬 강제는 운영 편의일 뿐. **A안 채택**: `ai_batch.py` 독립 모듈 추출 + scheduler에서 각 작업 `try` 독립화. 근거 — AI 일 1회 전제에서 B(완전 독립 2잡)의 유일 차별점인 "독립 케이던스"가 무가치해지고, A가 단일 컨테이너로 실패 격리·독립 재실행·관측 분리를 모두 달성하며 "잡 컨테이너 단일 인스턴스" 결정과 충돌하지 않음. C(현행)는 가격 실패 시 AI가 통째 스킵되는 실패 전파 결함 잔존. **B 승격 트리거**: 향후 AI를 가격과 다른 빈도(신규 크롤 직후·일 다회 등)로 돌릴 필요가 생길 때 — A 구조는 B 확장을 막지 않으므로 선제 채택 불요.

> 기능 D 구현 방식: 8-2 "재배치 중심" 채택 — AI 재실행 없이 저장된 user(`GameReviewSummary`)·critic(`CriticSummary`) 요약에서 괴리 재산출. `reduce_api.py` critic 프롬프트 반전(8-2)은 미적용(저비용·저리스크 우선, 8-5 부합).
> 5장·7-4는 본 9장으로 일원화됨. 기능 B는 AI 챗봇 파트 담당 — 범위 외.
