# Crawling

Steam(한국어 유저 리뷰) 및 Metacritic(영어 전문가 비평 리뷰)을 수집해 백엔드 API로 전송하는 크롤러

---

## 환경 설정

```bash
pip install -r crawling/requirements.txt
playwright install chromium   # Metacritic 크롤러용 (최초 1회)
```

---

## 전체 실행 순서

### 1단계 — 게임 목록 준비

`game_list.json`은 깃허브에 포함되어 있어 `git pull` 후 바로 사용 가능합니다.
목록을 새로 생성하거나 갱신할 때만 아래 스크립트를 실행합니다.

```bash
# 최초 생성 (Steam Top Sellers 100개 + metacritic_slug 자동 탐색)
python crawling/setup_game_list.py
python crawling/setup_game_list.py --auto-slug

# 목록 갱신 (기존 metacritic_slug 유지)
python crawling/setup_game_list.py --update --auto-slug
```

> `metacritic_slug`가 자동으로 채워지지 않은 게임은 `game_list.json`을 직접 편집합니다.
> 비어있는 게임은 Metacritic 크롤링에서 자동으로 스킵됩니다.

---

### 2단계 — 크롤링

두 크롤러는 독립적으로 실행 가능합니다 (순서 무관).

```bash
# Steam 한국어 리뷰 수집 (game_list.json의 steam_app_id 사용)
python crawling/steam/steam_crawler.py

# Metacritic 영어 전문가 비평 수집 (game_list.json의 metacritic_slug 사용)
python crawling/metacritic/metacritic_crawler.py
```

수집된 파일은 `crawling/output/steam.json`, `crawling/output/metacritic.json` 에 게임별로 합산 저장됩니다.
이미 수집된 게임(slug 키 존재)은 자동으로 스킵되어 이어서 실행할 수 있습니다.

> **Metacritic**: 전문가(critic) 리뷰만 수집합니다. 유저 리뷰는 수집 대상이 아닙니다.

---

### 3단계 — 백엔드 전송

크롤링은 **로컬 머신**에서 실행하고, 생성된 JSON 파일을 클라우드 서버로 옮겨 전송합니다.
클라우드 서버는 외부 IP를 지원하지 않으므로, 로컬에서 직접 API를 호출할 수 없습니다.

**파일 전송 방법 (웹 VSCode 이용):**

1. 로컬에서 크롤러 실행 후 생성된 JSON 파일 확인
   - `crawling/output/steam.json`
   - `crawling/output/metacritic.json`
2. 웹 VSCode 탐색기에서 `crawling/output/` 폴더에 우클릭 → **Upload...** 로 파일 업로드
3. 클라우드 터미널에서 전송 스크립트 실행

```bash
# 클라우드 터미널에서 실행
python crawling/send_to_api.py steam
python crawling/send_to_api.py metacritic
```

전송 성공한 파일은 자동으로 삭제됩니다. 실패한 파일은 남아있어 재실행 시 재전송됩니다.
`--keep` 옵션을 추가하면 전송 후에도 파일을 삭제하지 않습니다.

---

### 4단계 — AI 요약 실행

DB에 리뷰가 저장된 후 게임별로 AI 요약을 트리거합니다.

```bash
curl -X POST http://localhost:8000/api/v1/games/{game_id}/summarize

# 강제 재요약 (기존 요약 무시)
curl -X POST "http://localhost:8000/api/v1/games/{game_id}/summarize?force=true"
```

---

## Metacritic 셀렉터 깨짐 대응

Metacritic이 HTML 구조를 바꿔 리뷰가 제대로 수집되지 않을 경우, 아래 순서로 셀렉터를 자동 갱신합니다.

```bash
cd crawling/metacritic

# 1. 현재 DOM 구조 분석 → metacritic_inspect_result.json 생성
python inspector.py

# 2. JSON을 읽어 최적 셀렉터 추출 → metacritic_crawler.py 자동 패치
python auto_fix_selectors.py

# 3. 크롤러 재실행
python metacritic_crawler.py
```

`inspector.py`는 Chromium을 실제로 열어 DOM을 분석하므로, 실행 환경에 `playwright install chromium`이 완료되어 있어야 합니다.
패치 전 크롤러 파일은 자동으로 타임스탬프 백업됩니다(`metacritic_crawler.backup_YYYYMMDD_HHMMSS.py`).

---

## 파일 구조

```
crawling/
├── README.md
├── requirements.txt               # 크롤링 전용 의존성
├── game_list.json                 # 게임 목록 (git 관리, 팀 공유)
├── setup_game_list.py             # 게임 목록 생성·갱신 스크립트
├── send_to_api.py                 # 수집 파일 → 백엔드 전송
├── steam/
│   └── steam_crawler.py           # Steam 크롤러
└── metacritic/
    ├── metacritic_crawler.py      # Metacritic 전문가 리뷰 크롤러
    ├── inspector.py               # DOM 구조 분석 → metacritic_inspect_result.json
    └── auto_fix_selectors.py      # inspect 결과로 셀렉터 자동 패치
```

> `crawling/output/*.json`, `crawling/metacritic/metacritic_inspect_result.json`, `crawling/metacritic/*.backup_*.py` 은 `.gitignore` 처리되어 있습니다.
