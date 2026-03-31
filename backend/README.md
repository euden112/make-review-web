# 🎮 Game Review Aggregator - Backend

이 프로젝트는 Steam, Metacritic 등 다양한 플랫폼의 게임 리뷰 크롤링 데이터를 수신하여 데이터베이스에 저장(Upsert)하는 FastAPI 기반 백엔드 서버입니다.

## 📂 디렉토리 구조 및 파일 역할

''' text
backend/
├── requirements.txt         # FastAPI, SQLAlchemy, Pydantic 등 서버 구동에 필요한 패키지 목록
└── app/
    ├── main.py              # FastAPI 서버 진입점 (앱 초기화, CORS 설정, API 라우터 연결)
    ├── core/
    │   └── database.py      # PostgreSQL 비동기 데이터베이스 연결(세션/엔진) 설정
    ├── models/
    │   └── domain.py        # DB 테이블(games, external_reviews 등)과 매핑되는 SQLAlchemy ORM 모델
    ├── schemas/
    │   ├── metacritic.py    # Metacritic 크롤링 데이터 구조 및 타입 검증용 Pydantic 스키마
    │   └── steam.py         # Steam 크롤링 데이터 구조 및 타입 검증용 Pydantic 스키마
    └── api/v1/
        └── reviews.py       # 크롤러 데이터 수신(POST) 및 DB 중복 방지 저장(Bulk Upsert)을 담당하는 API 엔드포인트
```
