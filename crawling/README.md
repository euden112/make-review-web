# Crawling

Steam 및 Metacritic 게임 리뷰를 수집하고 FastAPI 서버로 전송하는 크롤러

⚙️ 설치

```bash
pip install requests httpx playwright
playwright install chromium
```

🚀 사용법

**1. 리뷰 수집**
```bash
python steam/steam_crawler.py
python metacritic/metacritic_crawler.py
```

**2. 서버 전송** (FastAPI가 `localhost:8000` 에서 실행 중이어야 합니다)
```bash
python send_to_api.py steam
python send_to_api.py metacritic
```
