"""
Steam Game Review Crawler
- crawling/game_list.json 에서 게임 목록 읽기 (steam_app_id 필드 사용)
- language=koreana + english: 한국어 + 영어 리뷰 수집 (게임당 각 언어 최대 MAX_REVIEWS_PER_GAME)
- 3-pool 전략: Pool1(헬프풀 긍정) + Pool2(헬프풀 부정) + Pool3(최신 전체)
- 게임당 200개 리뷰, 파일 단위 저장 (재시작 시 기존 파일 스킵)
- sentence_transformers 없음 — 한국어 키워드 매칭으로 카테고리 분류
"""

import re
import requests
import json
import time
import random
import argparse
import sys
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# ============================================================
# 설정
# ============================================================

MAX_REVIEWS_PER_GAME = 200
# 최신성·대표성 균형: 언어당 수집을 recent(최신순) + helpful(filter=all, 전기간 도움순)로
# 분할한다. recent만 쓰면 세일 유입·최근 패치 이슈로 편향되고, 명작의 핵심 호평(고-helpful
# 과거 리뷰)이 누락된다. 둘을 합쳐 seen으로 교차 중복 제거한다.
# 도움순 우세 배분: substance(길고 구체적·aspect 풍부한 핵심 리뷰) 확보를 우선한다.
# recent는 현재 여론 반영용으로 일부만 유지.
RECENT_PER_LANG  = 80
HELPFUL_PER_LANG = 120
MAX_BODY_LENGTH      = 1000
MIN_BODY_LENGTH      = 10
MAX_URLS             = 2

REVIEW_API_BASE = "https://store.steampowered.com/appreviews"
GAME_LIST_PATH  = Path(__file__).resolve().parent.parent / "game_list.json"
OUTPUT_DIR      = Path(__file__).resolve().parent.parent / "output"
OUT_FILE        = OUTPUT_DIR / "steam.json"

FALLBACK_GAMES = {
    "grand-theft-auto-v": {
        "steam_app_id": "271590",
        "steam_slug": "grand-theft-auto-v",
        "metacritic_slug": "grand-theft-auto-v",
        "name": "Grand Theft Auto V",
    },
    "elden-ring": {
        "steam_app_id": "1245620",
        "steam_slug": "elden-ring",
        "metacritic_slug": "elden-ring",
        "name": "ELDEN RING",
    },
}

# 카테고리 키워드 — 한국어 + 영어 (substring 매칭, 모두 lower-case 비교)
# 영어 키워드는 짧은 토큰("art", "bug" 등)이 오매치를 일으키지 않도록
# 충분히 변별력 있는 어구(보통 5자 이상 또는 합성어)만 채택.
GAME_CATEGORIES: dict[str, list[str]] = {
    "그래픽": [
        "그래픽", "비주얼", "화질", "아트", "그림체", "이펙트", "텍스처",
        "배경", "캐릭터 디자인", "예쁘", "아름답", "화려", "못생", "구리다",
        "조잡", "해상도", "렌더링",
        "graphics", "visual", "visuals", "art style", "art-style", "artstyle",
        "textures", "rendering", "resolution", "gorgeous", "stunning visuals",
        "beautiful game", "ugly graphics",
    ],
    "조작감": [
        "조작", "조작감", "컨트롤", "키보드", "마우스", "패드", "반응속도",
        "인풋렉", "입력 딜레이", "움직임", "이동감", "직관적", "어색하",
        "불편하", "자연스럽", "손맛",
        "controls", "control scheme", "keybinds", "key bindings", "input lag",
        "responsive controls", "clunky controls", "gamepad", "controller support",
        "mouse and keyboard", "movement feels",
    ],
    "최적화": [
        "최적화", "프레임", "프레임드랍", "버벅", "끊김", "렉", "로딩",
        "튕김", "크래시", "다운", "고사양", "저사양", "권장사양", "성능",
        "메모리", "cpu", "gpu", "fps",
        "optimization", "optimisation", "optimized", "poorly optimized",
        "unoptimized", "framerate", "frame rate", "fps drop", "low fps",
        "stuttering", "stutters", "performance issues", "crashes",
        "crash to desktop", "ctd", "loading times", "memory leak",
    ],
    "콘텐츠 양": [
        "콘텐츠", "볼륨", "플레이타임", "플레이 시간", "게임 시간",
        "엔드게임", "엔드컨텐츠", "후반부", "반복", "할 게 없", "금방 끝",
        "오래", "dlc", "업데이트", "신규 콘텐츠",
        "content", "endgame", "end-game", "end game", "playtime", "play time",
        "hours of content", "replayability", "replay value", "dlc",
        "expansion", "short game", "long game", "lots to do", "nothing to do",
        "grindy", "padded",
    ],
    "가성비": [
        "가성비", "가격", "할인", "세일", "환불", "비싸", "싸다", "저렴",
        "아깝", "돈 낭비", "정가", "원가", "지름",
        "price", "value for money", "bang for buck", "worth the price",
        "worth every penny", "overpriced", "not worth", "money's worth",
        "full price", "on sale", "discount", "refund", "waste of money",
    ],
    "스토리": [
        "스토리", "이야기", "서사", "세계관", "설정", "분위기", "캐릭터",
        "주인공", "npc", "감동", "몰입", "지루", "결말", "복선", "전개",
        "story", "narrative", "plot", "writing", "characters", "lore",
        "worldbuilding", "world-building", "world building", "atmosphere",
        "ending", "twist", "voice acting",
    ],
    "사운드": [
        "사운드", "음악", "bgm", "ost", "효과음", "성우", "더빙", "볼륨",
        "음질", "배경음",
        "soundtrack", "music", "sound design", "audio", "voice acting",
        "voice-acting", "voiceover", "sound effects", "sfx", "ambient sound",
        "dubbing",
    ],
    "난이도": [
        "난이도", "어렵", "쉽다", "도전적", "소울라이크", "죽음", "패널티",
        "보스", "고통", "뉴비", "입문",
        "difficulty", "difficult", "challenging", "punishing",
        "frustrating difficulty", "too easy", "too hard", "souls-like",
        "soulslike", "souls like", "git gud", "beginner friendly",
        "newcomer friendly",
    ],
    "멀티플레이": [
        "멀티", "협동", "코옵", "온라인", "pvp", "서버", "핑", "매칭",
        "대기", "파티",
        "multiplayer", "multi-player", "co-op", "coop", "pvp", "pve",
        "matchmaking", "online play", "server", "lobby", "party system",
        "cross-play", "crossplay",
    ],
    "재미": [
        "재미", "재밌", "노잼", "꿀잼", "갓겜", "명작", "중독성", "시간순삭",
        "손맛", "타격감", "전투", "게임성", "쾌감", "지루",
        "gameplay", "game play", "addictive", "addicting", "gripping",
        "gunplay", "combat feels", "game feel", "satisfying gameplay",
        "fun to play", "really fun", "so much fun", "boring gameplay",
        "core loop", "gameplay loop",
    ],
    "버그": [
        "버그", "오류", "에러", "충돌", "불안정", "패치", "수정", "먹통",
        "씹힘", "꼬임",
        "bugs", "buggy", "glitches", "glitchy", "broken mess",
        "game breaking", "game-breaking", "unfinished", "unstable",
        "needs patching", "needs a patch", "patched up",
    ],
}

NEGATIVE_KEYWORDS = {
    "별로", "최악", "쓰레기", "환불", "망겜", "구림",
    "불편", "실망", "후회", "돈낭비", "비추", "하지마",
    "형편없", "끔찍", "짜증", "노답", "최하", "망함",
    "garbage", "trash", "terrible", "awful", "horrible", "refund",
    "regret", "waste of money", "do not buy", "don't buy", "skip this",
    "skip it", "avoid", "disappointing", "disappointed", "broken mess",
    "uninstalled", "not worth", "do not recommend", "would not recommend",
    "not recommended", "boring", "worst",
}

# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class FilterResult:
    passed: bool
    stage: str
    reason: str
    categories: list[dict] = field(default_factory=list)

# ============================================================
# 게임 목록 로드
# ============================================================

def load_game_list() -> list[dict]:
    if not GAME_LIST_PATH.exists():
        print(f"[ERROR] game_list.json 없음: {GAME_LIST_PATH}")
        print("  → crawling/setup_game_list.py 를 먼저 실행하세요.")
        return []
    with open(GAME_LIST_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    valid = [e for e in entries if e.get("steam_app_id")]
    print(f"[게임 목록] game_list.json 에서 {len(valid)}개 로드")
    return valid

# ============================================================
# 유틸리티
# ============================================================

def make_slug(name: str) -> str:
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug or "unknown"

def get_image_urls(app_id: str) -> dict:
    _CDN_OLD  = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}"
    _CDN_NEW  = "https://shared.fastly.steamstatic.com/store_item_assets/"
    fallback_cover = f"{_CDN_OLD}/library_600x900.jpg"
    fallback_hero  = f"{_CDN_OLD}/header.jpg"

    try:
        import urllib.parse
        payload = {"ids": [{"appid": int(app_id)}], "context": {"country_code": "US", "language": "english", "steam_realm": 1}, "data_request": {"include_assets": True}}
        resp = requests.get(
            "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
            params={"input_json": json.dumps(payload)},
            timeout=10,
        )
        if resp.status_code == 200:
            items = resp.json().get("response", {}).get("store_items", [])
            if items:
                assets = items[0].get("assets", {})
                fmt = assets.get("asset_url_format", "")
                base = _CDN_NEW

                def _build(filename: str) -> str:
                    if fmt:
                        return base + fmt.replace("${FILENAME}", filename)
                    return f"{_CDN_OLD}/{filename}"

                capsule = assets.get("library_capsule")
                cover_image = _build(capsule) if capsule else fallback_cover

                hero_file = assets.get("library_hero") or assets.get("header")
                hero_image = _build(hero_file) if hero_file else fallback_hero

                return {"cover_image": cover_image, "hero_image": hero_image}
    except Exception:
        pass

    return {"cover_image": fallback_cover, "hero_image": fallback_hero}

def fetch_popular_tags(app_id: str, max_tags: int = 8) -> list[str]:
    """Steam store 페이지의 "이 제품의 인기 태그"(유저 정의 태그) 상위 N개를 수집한다.

    공식 appdetails JSON에는 인기 태그가 없고 genres(액션/RPG 수준의 coarse)만 있다.
    인기 태그(로그라이크/덱빌딩 등)는 store 페이지 HTML의 InitAppTagModal(appid, [...])
    배열에 임베드돼 있어 이를 파싱한다. SteamSpy는 신작에서 집계 지연으로 비어 있어
    store HTML이 더 신뢰도 높다. 성인 게임 연령게이트 우회용 쿠키를 함께 보낸다.
    LLM 키워드(리뷰 토픽, 가변)와 달리 장르를 일관되게 분리하는 신호로 쓴다.
    """
    try:
        resp = requests.get(
            f"https://store.steampowered.com/app/{app_id}/?cc=kr&l=koreana",
            headers={"Cookie": "birthtime=283993201; lastagecheckage=1-January-1990; wants_mature_content=1"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []
        m = re.search(r"InitAppTagModal\(\s*\d+\s*,\s*(\[.*?\])\s*,", resp.text, re.S)
        if not m:
            return []
        tags = json.loads(m.group(1))
        names = [str(t.get("name", "")).strip() for t in tags if isinstance(t, dict) and t.get("name")]
        return names[:max_tags]
    except Exception:
        return []

# ============================================================
# Steam 리뷰 API 호출 (페이지네이션)
# ============================================================

def fetch_raw_reviews(
    app_id: str,
    max_count: int,
    filter_type: str = "recent",
    review_type: str = "all",
    language: str = "koreana",
) -> tuple[list[dict], dict]:
    url     = f"{REVIEW_API_BASE}/{app_id}"
    reviews: list[dict] = []
    cursor  = "*"
    summary: dict = {}

    while len(reviews) < max_count:
        params = {
            "json"                    : 1,
            "language"                : language,
            "filter"                  : filter_type,
            "review_type"             : review_type,
            "purchase_type"           : "all",
            "num_per_page"            : min(100, max_count - len(reviews)),
            "cursor"                  : cursor,
            "filter_offtopic_activity": 1,
        }
        data: dict = {}
        for attempt in range(5):
            try:
                resp = requests.get(url, params=params, timeout=(5, 30))
                if resp.status_code == 429:
                    raise requests.RequestException("Rate Limit 429")
                resp.raise_for_status()
                data = resp.json()
                break
            except requests.RequestException as e:
                if attempt < 4:
                    backoff = min(30, (2 ** attempt) + random.uniform(0, 1))
                    print(f"    [WARN] 재시도 {attempt+1}/5 ({e}) — {backoff:.1f}s 대기")
                    time.sleep(backoff)
                else:
                    print(f"    [ERROR] API 최종 실패: {e}")

        if data.get("success") != 1:
            break
        if not summary:
            summary = data.get("query_summary", {})

        fetched = data.get("reviews", [])
        if not fetched:
            break

        reviews.extend(fetched)
        cursor = data.get("cursor", "")
        if not cursor:
            break

    return reviews[:max_count], summary

# ============================================================
# 전처리
# ============================================================

def preprocess_body(text: str) -> str | None:
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(
        r"[\U00010000-\U0010ffff\U0001F600-\U0001F64F"
        r"\U0001F300-\U0001F5FF\U0001F680-\U0001F6FF\U0001F1E0-\U0001F1FF]+",
        "", text, flags=re.UNICODE,
    )
    text = re.sub(r"([^\w\s])\1{2,}", r"\1", text)
    text = re.sub(r" {2,}", " ", text).strip()
    if len(text) < MIN_BODY_LENGTH:
        return None
    if len(text) > MAX_BODY_LENGTH:
        cut = text[:MAX_BODY_LENGTH]
        m = re.search(r"[.!?~][^.!?~]*$", cut)
        text = cut[:m.start() + 1].strip() if m else cut.strip()
    return text

# ============================================================
# 필터 파이프라인
# ============================================================

def rule_based_filter(text: str) -> FilterResult:
    if len(re.findall(r"https?://", text)) >= MAX_URLS:
        return FilterResult(False, "rule", "spam_url")
    return FilterResult(True, "rule", "pass")

def korean_spam_filter(text: str) -> FilterResult:
    # 자모만 나열된 텍스트 제거 (ㅋㅋㅋ, ㅠㅠㅠ 등)
    jamo_chars = len(re.findall(r"[ㄱ-ㅎㅏ-ㅣ]", text))
    total_chars = len(text.replace(" ", ""))
    if total_chars > 0 and jamo_chars / total_chars > 0.5:
        return FilterResult(False, "korean_spam", "jamo_only")

    return FilterResult(True, "korean_spam", "pass")

def _detect_sentiment(sentence: str) -> str:
    lower = sentence.lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw in lower:
            return "negative"
    return "positive"

def category_tag(text: str) -> list[dict]:
    lower = text.lower()
    sentences = re.split(r"(?<=[.!?~])\s+", text)
    if not sentences:
        sentences = [text]

    matched: dict[str, str] = {}
    for cat, keywords in GAME_CATEGORIES.items():
        for sentence in sentences:
            if cat in matched:
                break
            sl = sentence.lower()
            for kw in keywords:
                if kw in sl:
                    matched[cat] = _detect_sentiment(sentence)
                    break

    return [{"category": c, "sentiment": s} for c, s in matched.items()]

def run_filter_pipeline(text: str) -> FilterResult:
    r = rule_based_filter(text)
    if not r.passed:
        return r
    r = korean_spam_filter(text)
    if not r.passed:
        return r
    cats = category_tag(text)
    return FilterResult(True, "pass", "pass", categories=cats)

# ============================================================
# 개별 리뷰 파싱
# ============================================================

def parse_review(raw: dict) -> dict | None:
    author = raw.get("author", {})

    body = preprocess_body(raw.get("review", ""))
    if body is None:
        return None

    result = run_filter_pipeline(body)
    if not result.passed:
        return None

    ts   = raw.get("timestamp_created", 0)
    date = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else ""

    return {
        "author_id"        : author.get("steamid", ""),
        "is_recommended"   : raw.get("voted_up", False),
        "review_text"      : body,
        "playtime_hours"   : round(author.get("playtime_at_review", 0) / 60, 1),
        "helpful_count"    : int(raw.get("votes_up", 0) or 0),
        "date_posted"      : date,
        "language"         : raw.get("language", "koreana"),
        "review_categories": result.categories,
    }

# ============================================================
# 파싱 + 중복 제거 헬퍼
# ============================================================

def parse_and_dedup(
    raw_list: list[dict],
    seen: set[str],
    pool_label: str,
    slug: str,
) -> list[dict]:
    collected = []
    skipped   = 0

    for raw in raw_list:
        rid = str(raw.get("recommendationid", ""))
        if rid and rid in seen:
            skipped += 1
            continue

        parsed = parse_review(raw)
        if parsed is None:
            skipped += 1
            if rid:
                seen.add(rid)
            continue

        dedup_key = rid or parsed["review_text"][:50]
        if dedup_key in seen:
            skipped += 1
            continue

        seen.add(dedup_key)
        collected.append(parsed)

    print(
        f"    [{slug}] {pool_label}: 원본 {len(raw_list)}개 "
        f"| 필터/중복 {skipped}개 | 저장 {len(collected)}개"
    )
    return collected

# ============================================================
# 게임 단위 수집 (3-pool 전략)
# ============================================================

def collect_game(slug: str, app_id: str, name: str, game_list_id: int | None = None) -> dict:
    print(f"  [{slug}] 수집 시작 (app_id={app_id})")

    images = get_image_urls(app_id)
    tags = fetch_popular_tags(app_id)
    all_reviews: list[dict] = []
    seen: set[str] = set()

    for lang in ("koreana", "english"):
        print(
            f"    [{slug}] {lang} recent {RECENT_PER_LANG} + helpful {HELPFUL_PER_LANG} 수집"
        )
        # 1) 최신순(recent): 현재 여론·최근 패치 반영
        raw_recent, _ = fetch_raw_reviews(
            app_id,
            max_count=RECENT_PER_LANG,
            filter_type="recent",
            review_type="all",
            language=lang,
        )
        # 2) 도움순(filter=all, 전기간): 대표성 — 오래됐어도 핵심 호평/비판 확보
        raw_helpful, _ = fetch_raw_reviews(
            app_id,
            max_count=HELPFUL_PER_LANG,
            filter_type="all",
            review_type="all",
            language=lang,
        )
        # recent 먼저 dedup 등록 → helpful은 seen으로 중복 제거(겹치면 recent 유지)
        all_reviews.extend(parse_and_dedup(raw_recent, seen, f"{lang} 최신", slug))
        all_reviews.extend(parse_and_dedup(raw_helpful, seen, f"{lang} 도움순", slug))

    print(f"  [{slug}] 완료 → {len(all_reviews)}개 저장")

    return {
        slug: {
            "meta": {
                "game_list_id": game_list_id,
                "game_id"     : app_id,
                "name_ko"     : name,
                "cover_image" : images["cover_image"],
                "hero_image"  : images["hero_image"],
                "tags"        : tags,
                "crawled_at"  : datetime.now().isoformat(),
            },
            "reviews": all_reviews,
        }
    }

# ============================================================
# 메인
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="Steam review crawler")
    parser.add_argument(
        "--games",
        nargs="*",
        default=None,
        help="Limit crawl to matching steam_slug, metacritic_slug, generated slug, or name",
    )
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    existing_data: dict = {}
    if OUT_FILE.exists():
        with open(OUT_FILE, encoding="utf-8") as f:
            existing_data = json.load(f)

    entries = load_game_list()
    if args.games:
        requested = {g.strip().lower() for g in args.games if g.strip()}
        entries = [
            entry for entry in entries
            if (
                str(entry.get("steam_slug", "")).lower() in requested
                or str(entry.get("metacritic_slug", "")).lower() in requested
                or make_slug(str(entry.get("name", ""))).lower() in requested
                or str(entry.get("name", "")).lower() in requested
            )
        ]
        matched = {
            value
            for entry in entries
            for value in (
                str(entry.get("steam_slug", "")).lower(),
                str(entry.get("metacritic_slug", "")).lower(),
                make_slug(str(entry.get("name", ""))).lower(),
                str(entry.get("name", "")).lower(),
            )
        }
        for slug in sorted(requested - matched):
            fallback = FALLBACK_GAMES.get(slug)
            if fallback:
                entries.append(fallback)
                matched.update(
                    str(fallback.get(key, "")).lower()
                    for key in ("steam_slug", "metacritic_slug", "name")
                )
        missing = requested - {
            value
            for entry in entries
            for value in (
                str(entry.get("steam_slug", "")).lower(),
                str(entry.get("metacritic_slug", "")).lower(),
                make_slug(str(entry.get("name", ""))).lower(),
                str(entry.get("name", "")).lower(),
            )
        }
        if missing:
            print(f"[WARN] unmatched games: {', '.join(sorted(missing))}")

    if not entries:
        return

    print("\n" + "=" * 60)
    print(f"  총 게임 수    : {len(entries)}")
    print(f"  게임당 최대   : {MAX_REVIEWS_PER_GAME}개")
    print(f"  언어          : koreana + english (한국어 + 영어)")
    print(f"  저장 위치     : {OUT_FILE}")
    print(f"  기존 수집     : {len(existing_data)}개 게임")
    print("=" * 60 + "\n")

    success, skipped_count, failed = [], [], []

    for i, entry in enumerate(entries, 1):
        app_id        = entry["steam_app_id"]
        name          = entry.get("name", app_id)
        slug          = entry.get("steam_slug") or make_slug(name)
        game_list_id  = entry.get("id")

        print(f"[{i:3d}/{len(entries)}] {name} ({slug})")

        if slug in existing_data:
            print(f"  → 이미 수집됨, 스킵: {slug}")
            skipped_count.append(slug)
            continue

        try:
            result = collect_game(slug, app_id, name, game_list_id)
            existing_data.update(result)
            with open(OUT_FILE, "w", encoding="utf-8") as f:
                json.dump(existing_data, f, ensure_ascii=False, indent=2)
            review_count = len(result[slug]["reviews"])
            print(f"  → 저장 완료: {slug} ({review_count}개)\n")
            success.append(slug)
        except Exception as e:
            print(f"  → [ERROR] {slug} 실패: {e}\n")
            failed.append(slug)

        time.sleep(2.0)

    print("\n" + "=" * 60)
    print("크롤링 완료 요약")
    print(f"  성공  : {len(success)}개")
    print(f"  스킵  : {len(skipped_count)}개 (이미 수집됨)")
    print(f"  실패  : {len(failed)}개")
    if failed:
        print(f"  실패 목록: {', '.join(failed)}")
    print("=" * 60)


if __name__ == "__main__":
    main()
