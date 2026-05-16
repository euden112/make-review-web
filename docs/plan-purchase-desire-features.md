# 구매 욕구 유발 기능 기획서 (A·C)

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
  "sale_ends_at": "2026-05-25",
  "sentiment_state": "positive_recovery",
  "reasons": ["50% 할인 중", "최근 평가가 역대 최고 구간", "세일 종료 D-9"]
}
```

목록 페이지 일괄 조회 부담을 줄이기 위해 게임 목록 응답에 경량 플래그(`is_good_timing`, `discount_percent`)만 포함하고, 상세 사유는 본 엔드포인트로 분리한다.

### 3-5. UI

- `GameListPage` 카드: "지금이 적기" 배지 + 할인율
- `GameDetailPage` 히어로: 사유 + 세일 종료 카운트다운 (FOMO)

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

| 순위 | 작업 | 비용 | 근거 |
|---|---|---|---|
| 1 | C. 감성 하이라이트 | 낮음 (선별 알고리즘 + 엔드포인트) | 신규 수집 0, 즉효성 |
| 2 | A. 구매 타이밍 시그널 — `appdetails` 수집 추가 | 중간 | 신규 API 1개 |
| 3 | A. 판정 로직 + signal API | 중간 | 이슈 트래킹 코드 이관·반전 |
| 4 | A·C 프론트엔드 (배지·카운트다운·캐러셀) | 중간 | UI 통합 |
| 5 | 이슈 트래킹 폐지 정리 (`events.py` 제거, `GameEvent` 처리) | 낮음 | 자산 이관 완료 후 |

---

## 6. 폐지·전환 요약

- 이슈 트래킹 폐지로 `histogram_crawler`·`news_crawler`는 버려지지 않고 기능 A로 이관
- 기능 C는 기존 `ExternalReview`·`aspect_scores` 자산을 재선별만으로 욕구 유발 자산화
- 두 기능 모두 신규 대규모 수집 없이 2차(구매 욕구 유발) 공백을 메움
- B(발견 피드)는 AI 챗봇 파트가 별도 담당하여 2차 목표를 입구·상세·추천 3면에서 커버

---

## 7. 브랜치 재정비 및 작업 로드맵

### 7-1. 배경 및 기준점 정정

기존 분기 구조에서 `feature/issue-tracking-test`가 가장 앞서 있었으나, 이슈 트래킹 폐지와 방향 전환에 따라 **실제 공유 베이스에서 다시 시작**한다.

**기준점 정정 (중요):** 초기에는 공통 조상을 `7c592a4`로 잡았으나, 검증 결과 잘못된 전제였다.

```
7c592a4 "frontend Docker 설정"  = 로컬 feature/playtime-critic-user-analysis (stale)
   └─> 6a97e24 "fix(sprint4): DB 타입·하드코딩·마이그레이션 수정"
        = origin/feature/playtime-critic-user-analysis (원격 tip)
        = ai-chatbot · cloud-deploy 의 실제 베이스
        ├── feature/issue-tracking-test
        ├── ai-chatbot
        └── cloud-deploy
```

- 팀이 원격에서 실제로 작업을 쌓은 진짜 공통 베이스는 **`6a97e24`** (7c592a4 아님)
- `6a97e24`는 Sprint4 DB 정합성 수정 포함: `playtime_analyses`/`critic_summaries` FLOAT→NUMERIC·INTEGER→BIGINT, API `tags`/`rating` 필드 추가 — **기능 C·D와 직접 관련**
- 로컬 `feature/playtime-critic-user-analysis`는 원격보다 1커밋 뒤(stale), fast-forward 가능

**확정 기준점: `6a97e24` (= origin/feature/playtime-critic-user-analysis)**

### 7-2. 완료된 작업 (코드 검증 기준 2026-05-17, rebase 후 갱신)

작업은 신규 브랜치 **`feature/purchase-desire`** (베이스 `6a97e24`)에서 진행됨.

| 항목 | 상태 | 근거 |
|---|---|---|
| AI 파이프라인 CPU 최적화 이관 | ✅ 완료 | `930ea5f`, `cc66671` |
| 번역 API·게임 비교 이관 | ✅ 완료 | `299332f`, `3c98276`, `translate.py` |
| 이슈 트래킹 폐지 | ✅ 완료 | `events.py`·`GameEvent`·`EventSummary`·관련 SQL 제거, `b1a2308` 데모 재편 |
| 기능 C — 감성 하이라이트 (API) | ✅ 완료 | `a0996bc`, `highlights.py` 라우팅됨 |
| 기능 A — appdetails 수집 | ✅ 완료 | `d902f2e`, `appdetails_crawler.py` |
| 기능 A — buy-signal API | ✅ 완료 | `fbd23c5`, `buy_signal.py` 라우팅됨 |
| A·C 프론트엔드 | ✅ 완료 | `a66d787` (배지+명장면 캐러셀) |

### 7-3. 기준점 정렬 — 완료

기준점 불일치(`feature/purchase-desire`가 옛 `7c592a4` 기반)는 **`6a97e24` 위로 rebase 하여 해소됨**.

| 항목 | 상태 | 비고 |
|---|---|---|
| `feature/purchase-desire` → `6a97e24` rebase | ✅ 완료 | 15커밋 재적용, 충돌 3파일 해결 |
| 충돌 해결: `summaries.py` | ✅ | `6a97e24` tags/rating(Metacritic 환산) + purchase-desire 배치 쿼리 최적화 **병합** |
| 충돌 해결: `GameListPage.jsx` | ✅ | API `g.tags`(GAME_META 제거) + 대소문자 무시 검색 + buy-signal 배지 병합 |
| 충돌 해결: `GameDetailPage.jsx` | ✅ | API `game.rating` 사용(제거된 `meta.rating` 폐기) + buy-signal 배너 보존 |
| Sprint4 DB 정합성 수정 포함 | ✅ | `09_migration_sprint4_fixes.sql` 등 베이스에 포함 확인 |
| 백업 ref | ✅ | `backup/purchase-desire-prerebase` (= 구 `a66d787`) 롤백 안전망 |

### 7-4. 남은 작업

| 순위 | 작업 | 분류 | 상태 | 비고 |
|---|---|---|---|---|
| 1 | 기능 C 동작 검증 (rebase 후 회귀 확인) | 검증 | ⬜ | highlights API + 명장면 캐러셀 정상 동작 확인 |
| 2 | 프론트엔드 빌드/렌더 검증 | 검증 | ⬜ | 충돌 해결한 2 JSX 파일 실제 렌더 확인 (dev server) |
| 3 | 기능 D — 유저/평론 분리 요약 (API/파이프라인) | 신규 (1차) | ⬜ | 섹션 8. 베이스 정렬 완료로 착수 가능 |
| 4 | 기능 D 프론트엔드 (2트랙 패널·괴리 지표) | 신규 (1차) | ⬜ | 8-4 동적·비대칭 노출 |
| 5 | 로컬 `main`·`playtime`·`origin/main` 정렬 | git·의사결정 | ⬜ | `origin/main` push 시 force 필요(원격 `644e4a7` 삭제) — 승인 필요 |

> 기능 B(발견 피드)는 AI 챗봇 개발 파트가 별도 담당 — 본 로드맵 범위 외.

### 7-5. 자산 이관 주의사항

- 자산 이관(AI 최적화·번역·게임 비교)·이슈 트래킹 폐지·기능 A·C·기준점 rebase는 **완료** (7-2, 7-3)
- `game_events` 타입 버그는 이슈 트래킹 폐지로 자연 해소됨 (완료)
- rebase 충돌 해결 시 `6a97e24` 의도(GAME_META 제거→API tags/rating) 우선 적용 — purchase-desire의 dead `meta.rating` 참조는 폐기됨. 프론트 렌더 검증으로 회귀 없음 확인 필요 (7-4 #2)

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
