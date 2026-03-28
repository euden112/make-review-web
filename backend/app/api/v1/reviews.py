from fastapi import APIRouter
from typing import Dict
from app.schemas.metacritic import MetacriticPayload
from app.schemas.steam import SteamPayload

router = APIRouter()

@router.post("/metacritic")
async def receive_metacritic_data(payload: Dict[str, MetacriticPayload]):
    """
    [POST] 크롤러로부터 메타크리틱 리뷰 데이터를 수신합니다.
    - 형식: {"게임이름": {"meta": {...}, "reviews": [...]}}
    """
    for game_name, game_data in payload.items():
        print(f"[Metacritic 수신 완료] {game_name}")
        print(f" - 전문가 리뷰: {game_data.meta.critic_count}개 / 유저 리뷰: {game_data.meta.user_count}개")
        
    return {
        "status": "success",
        "message": f"메타크리틱 데이터 {len(payload)}건 수신 완료"
    }

@router.post("/steam")
async def receive_steam_data(payload: Dict[str, SteamPayload]):
    """
    [POST] 스팀 Web API / 크롤러로부터 스팀 리뷰 데이터를 수신합니다.
    - 형식: {"게임이름": {"meta": {...}, "reviews": [...]}}
    """
    for game_name, game_data in payload.items():
        print(f"[Steam 수신 완료] {game_name}")
        print(f" - 긍정: {game_data.meta.total_positive}개 / 부정: {game_data.meta.total_negative}개")
        
    return {
        "status": "success",
        "message": f"스팀 데이터 {len(payload)}건 수신 완료"
    }