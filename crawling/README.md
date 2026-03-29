Crawling

파일 설명

metacritic_crawler.py : 메타크리틱 리뷰 크롤러

steam_crawler.py : 스팀 공식 API 기반 유저 리뷰 크롤러
- 게임 5개(gta5, 엘든링, 배그, 33원정대, 붉은 사막), 유저 리뷰 50개
- 전처리 포함 (최소 20자 이상, 최대 500자 truncate, 중복 제거)
- 결과: reviews_steam.json 저장

reviews_metacritic.json : 수집된 리뷰 원본 데이터 - 게임 5개(gta5, 엘든링, 배그, 33원정대, 붉은 사막), 각 전문가/유저 50개

reviews_steam.json : 스팀에서 수집된 리뷰 원본 데이터
- 게임 5개(gta5, 엘든링, 배그, 33원정대, 붉은 사막), 각 유저 리뷰 50개

send_to_api_steam.py : 스팀 리뷰 데이터 전송
send_to_api_metacritic.py : 메타크리틱 리뷰 데이터 전송
- reviews_steam.json → FastAPI /api/v1/reviews/steam 으로 전송
