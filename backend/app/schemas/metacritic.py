from pydantic import AliasChoices, BaseModel, Field
from typing import Dict, List, Optional, Union

# 1. 크롤러가 보내는 메타크리틱 리뷰 1개의 구조를 정의합니다.
class MetacriticReview(BaseModel):
    author: str = Field(description="리뷰 작성자")
    score: str = Field(description="부여한 점수 (문자열)")
    body: str = Field(description="리뷰 본문")
    date: str = Field(description="작성 날짜")
    type: str = Field(description="critic 또는 user")
    language: Optional[str] = Field(default="en", validation_alias=AliasChoices("language", "lang"), description="리뷰 작성 언어")
    helpful_count: Optional[int] = Field(default=0, description="도움됨 투표 수")
    review_categories: Optional[List[Union[str, Dict]]] = Field(
        default_factory=list,
        description="리뷰 카테고리 배열. 문자열 배열 ['그래픽'] 또는 객체 배열 [{category, sentiment}] 모두 허용",
    )

# 2. 메타크리틱 통계 정보 구조입니다. (정제 파이프라인에서 걸러진 개수 필드 포함)
class MetacriticMeta(BaseModel):
    game: str
    platform: str
    crawled_at: str
    total: int
    critic_count: int
    game_list_id: Optional[int] = Field(default=None, description="game_list.json 기준 게임 고유 ID")
    user_count: int = Field(default=0, description="유저 리뷰 수 (현재 미수집)")
    filtered_count: Optional[int] = Field(default=None, description="필터링 후 남은 리뷰 수")

# 3. 크롤러가 최종 전송할 때 사용하는 포장지 구조입니다.
class MetacriticPayload(BaseModel):
    meta: MetacriticMeta
    reviews: List[MetacriticReview]