from pydantic import BaseModel, Field
from typing import List

# 1. 개별 리뷰 데이터 (메타크리틱)
class MetacriticReview(BaseModel):
    author: str = Field(description="리뷰 작성자")
    score: str = Field(description="부여한 점수 (문자열)")
    body: str = Field(description="리뷰 본문")
    date: str = Field(description="작성 날짜")
    type: str = Field(description="critic 또는 user")

# 2. 게임 메타데이터 (메타크리틱)
class MetacriticMeta(BaseModel):
    game: str
    platform: str
    crawled_at: str
    total: int
    critic_count: int
    user_count: int
    filtered_count: Optional[int] = Field(default=None, description="필터링 후 남은 리뷰 수")
# 3. 크롤러가 전송하는 최종 형태
class MetacriticPayload(BaseModel):
    meta: MetacriticMeta
    reviews: List[MetacriticReview]