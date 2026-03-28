from pydantic import BaseModel, Field
from typing import List

# 1. 개별 리뷰 데이터 (스팀)
class SteamReview(BaseModel):
    author_id: str = Field(description="스팀 유저 고유 ID")
    is_recommended: bool = Field(description="추천 여부 (True=긍정, False=부정)")
    review_text: str = Field(description="리뷰 본문")
    playtime_hours: float = Field(description="해당 게임 플레이 타임 (시간)")
    date_posted: str = Field(description="작성 날짜")

# 2. 게임 메타데이터 (스팀)
class SteamMeta(BaseModel):
    game_id: str = Field(description="스팀 앱 ID 또는 게임명")
    total_positive: int = Field(description="총 긍정 리뷰 수")
    total_negative: int = Field(description="총 부정 리뷰 수")
    crawled_at: str

# 3. 스팀 데이터 수집기가 전송하는 최종 형태
class SteamPayload(BaseModel):
    meta: SteamMeta
    reviews: List[SteamReview]