from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import reviews

# FastAPI 애플리케이션 객체를 생성합니다. 이 객체가 전체 웹 서버의 중심이 됩니다.
app = FastAPI(
    title="Game Review Aggregator API",
    description="플랫폼별(스팀, 메타크리틱) 리뷰 수집 및 요약 백엔드",
    version="1.0.0"
)

# CORS(교차 출처 리소스 공유) 설정입니다.
# 웹 브라우저에서 다른 도메인(예: React로 만든 프론트엔드 화면)이 이 백엔드 서버의 API를 호출할 수 있도록 허용해 줍니다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 실제 배포 시에는 보안을 위해 특정 프론트엔드 주소(예: ["http://localhost:3000"])만 허용해야 합니다.
    allow_credentials=True,
    allow_methods=["*"],  # GET, POST, PUT, DELETE 등 모든 HTTP 통신 방식을 허용합니다.
    allow_headers=["*"],  # 모든 HTTP 헤더를 허용합니다.
)

# 만들어둔 리뷰 수신용 라우터(API 엔드포인트들)를 메인 앱에 등록하여 연결해 줍니다.
# 이제 "/api/v1/reviews" 주소로 들어오는 요청은 reviews.py 파일에서 처리하게 됩니다.
app.include_router(reviews.router, prefix="/api/v1/reviews", tags=["Reviews Data Ingestion"])

# 서버가 잘 켜졌는지 확인하기 위한 기본(Root) 경로입니다.
# 브라우저에서 http://localhost:8000 에 접속하면 아래 메시지가 보입니다.
@app.get("/")
async def root():
    return {"message": "서버 정상 구동 중. API 문서는 /docs 에서 확인하세요."}