# 🎮 Game Review Aggregator - Backend

이 프로젝트는 Steam, Metacritic 등 다양한 플랫폼의 게임 리뷰 크롤링 데이터를 수신하여 데이터베이스에 저장(Upsert)하는 FastAPI 기반 백엔드 서버입니다.

## 📂 디렉토리 구조 및 파일 역할


```text
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

#### 1단계: 백엔드 폴더로 이동
터미널을 열고 프로젝트의 백엔드 디렉토리로 진입합니다.
\`\`\`bash
cd backend
\`\`\`

#### 2단계: 필수 패키지 직접 설치
가상환경을 켜지 않은 상태에서 아래 설치 명령어를 입력합니다. (내 컴퓨터의 전역 파이썬 환경에 직접 설치됩니다.)
\`\`\`bash
pip install -r requirements.txt
\`\`\`
*(설치가 완료될 때까지 잠시 기다려주세요.)*

#### 3단계: 서버 실행
패키지 설치가 끝났다면 바로 서버 구동 명령어를 실행합니다.
\`\`\`bash
uvicorn app.main:app --reload
\`\`\`

---

#### ✅ 접속 및 확인
1. 터미널 창에 `Application startup complete.` 메시지가 출력되면 서버가 정상적으로 켜진 것입니다.
2. 웹 브라우저를 열고 Swagger UI 주소인 [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) 로 접속하여 API 문서가 잘 뜨는지 확인합니다.
3. *실행 중 에러가 발생할 경우, 터미널에 출력된 에러 로그를 확인
