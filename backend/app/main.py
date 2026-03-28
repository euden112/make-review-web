from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.v1 import reviews

app = FastAPI(
    title="Game Review Aggregator API",
    description="플랫폼별(스팀, 메타크리틱) 리뷰 수집 및 요약 백엔드",
    version="1.0.0"
)

# CORS 설정 (React 프론트엔드 통신 허용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 리뷰 라우터 등록
app.include_router(reviews.router, prefix="/api/v1/reviews", tags=["Reviews Data Ingestion"])

@app.get("/")
async def root():
    return {"message": "서버 정상 구동 중. API 문서는 /docs 에서 확인하세요."}