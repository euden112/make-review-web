from pydantic import AliasChoices, BaseModel, Field
from typing import List, Optional

# 1. 크롤러가 보내는 스팀 리뷰 1개의 구조를 정의합니다.
class SteamReview(BaseModel):
    author_id: str = Field(description="스팀 유저 고유 ID")
    is_recommended: bool = Field(description="추천 여부 (True=긍정, False=부정)")
    review_text: str = Field(description="리뷰 본문")
    playtime_hours: float = Field(description="해당 게임 플레이 타임 (시간)")
    date_posted: str = Field(description="작성 날짜")
    language: Optional[str] = Field(default="en", validation_alias=AliasChoices("language", "lang"), description="리뷰 작성 언어")
    helpful_count: Optional[int] = Field(default=0, description="도움됨 투표 수")
    review_categories: Optional[List[str]] = Field(default_factory=list, description="리뷰 카테고리")

# 2. 게임 한 개당 같이 딸려오는 스팀 통계 정보 구조를 정의합니다.
class SteamMeta(BaseModel):
    game_id: str = Field(description="스팀 앱 ID 또는 게임명")
    total_positive: int = Field(description="총 긍정 리뷰 수")
    total_negative: int = Field(description="총 부정 리뷰 수")
    crawled_at: str

# 3. 크롤러가 최종적으로 백엔드에 전송할 때 사용하는 전체 데이터 포장지 구조입니다.
class SteamPayload(BaseModel):
    meta: SteamMeta
    reviews: List[SteamReview]