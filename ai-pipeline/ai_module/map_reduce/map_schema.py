from __future__ import annotations

import json
import re
from typing import Any


ALLOWED_ASPECTS = {
    "graphics",
    "controls",
    "optimization",
    "content",
    "price_value",
    "sound",
    "gameplay",
    "difficulty",
}

# 게임 무관(genre-agnostic) 일반 스포일러 카테고리어만 둔다. 특정 게임 고유명사
# (보스명·지역명·엔딩명 등)는 하드코딩하면 한 게임에만 편향되고 50게임 확장도 안 되므로
# 두지 않는다. 게임별 고유 스포일러는 Map LLM이 출력하는 evidence의 spoiler_terms가 담당한다.
SPOILER_TERM_PATTERNS = {
    "final_boss": ("final boss", "last boss", "최종 보스", "최종보스", "막보"),
    "ending": ("ending", "true ending", "bad ending", "엔딩", "진엔딩", "배드엔딩"),
    "twist": ("plot twist", "twist", "반전", "정체", "배신"),
    "death": ("dies", "death of", "killed", "사망", "죽는다", "죽음"),
    "late_area": ("late-game area", "endgame area", "후반 지역", "후반부 지역", "엔드게임 지역"),
    "quest_resolution": ("quest ending", "questline ending", "퀘스트 결말", "퀘스트 엔딩"),
}

SPOILER_RISKS = {"none", "low", "medium", "high"}


def safe_parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    raw = re.sub(r"^```[a-zA-Z]*\s*", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return parsed


def _string_list(value: Any, *, max_items: int = 12) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:max_items]:
        text = str(item).strip()
        if text:
            result.append(text)
    return result


def _int_list(value: Any, allowed_ids: set[int] | None = None) -> list[int]:
    if not isinstance(value, list):
        return []
    result: list[int] = []
    for item in value:
        try:
            item_id = int(item)
        except (TypeError, ValueError):
            continue
        if allowed_ids is not None and item_id not in allowed_ids:
            continue
        if item_id not in result:
            result.append(item_id)
    return result


def _normalize_source(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"steam_user", "metacritic_user", "metacritic_critic"}:
        return text
    return "steam_user"


def _normalize_polarity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in {"positive", "mixed", "negative"}:
        return text
    return "mixed"


def _spoiler_terms_from_text(text: str) -> list[str]:
    lower = (text or "").lower()
    terms: list[str] = []
    for patterns in SPOILER_TERM_PATTERNS.values():
        for term in patterns:
            if term.lower() in lower and term not in terms:
                terms.append(term)
    return terms


def _normalize_spoiler_risk(value: Any, *, terms: list[str]) -> str:
    text = str(value or "").strip().lower()
    if text in SPOILER_RISKS:
        return text
    return "medium" if terms else "none"


def _redact_spoiler_terms(text: str, terms: list[str]) -> str:
    result = " ".join(str(text or "").split())
    for term in sorted(terms, key=len, reverse=True):
        if not term:
            continue
        result = re.sub(re.escape(term), "후반부 핵심 요소", result, flags=re.I)
    return result


# 표시용 대표 리뷰(원문 verbatim)에 적용하는 강한 비속어 — 마스킹 처리.
# 긴 패턴을 먼저 두어 부분 중복 치환을 피한다.
_DISPLAY_PROFANITY_PATTERNS = (
    "좆같", "좆", "씨1발", "씨발", "시발", "ㅅㅂ", "ㅄ", "ㅈㄴ", "ㅈ같", "존나", "개같",
    # 숫자 삽입 난독화 변형 포함
    "병1신", "병2신", "병신", "정2병", "정1병", "새끼",
)


def redact_display_text(text: str) -> str:
    """표시용 대표 리뷰 경량 redaction.

    요약용 sanitizer(_sanitize_public_text)는 filler 치환까지 해서 원문을 많이 바꾸므로
    verbatim 표시에는 부적합하다. 표시용은 원문 가치를 최대한 보존하되 (1)게임 무관 일반
    스포일러 패턴 치환, (2)강한 비속어 마스킹만 적용한다. 게임 고유명사는 패턴 사전에
    없으므로 redaction되지 않는다(설계상 LLM 요약 본문에서만 spoiler_terms로 처리).
    """
    raw = str(text or "")
    result = _redact_spoiler_terms(raw, _spoiler_terms_from_text(raw))
    for term in _DISPLAY_PROFANITY_PATTERNS:
        result = re.sub(re.escape(term), "***", result, flags=re.I)
    return result


def _prefer_source_language_detail(detail: str, snippet: str) -> str:
    """로컬 LLM이 detail을 원문과 다른 언어로 번역한 경우 원문 snippet으로 보정한다.

    qwen 계열은 한국어/영어 리뷰의 detail을 중국어로 번역하거나 한자를 섞어
    출력하기도 한다(한국어 critic 요약 입력으로 부적합). 따라서 detail에 한자(中文)가
    있는데 원문 snippet에는 한자가 없으면(원문이 한국어/영어인데 중국어로 오염된 경우)
    원문 snippet을 detail로 사용한다. snippet 자체가 한자를 포함한 진짜 중국어 리뷰는
    보정하지 않는다. 영어 detail은 한자가 없으므로 그대로 통과한다.
    """
    if re.search(r"[一-鿿]", detail) and not re.search(r"[一-鿿]", snippet):
        return snippet
    return detail


def _public_detail_from_item(item: dict[str, Any], detail: str, snippet: str) -> tuple[str, str, list[str]]:
    explicit_terms = _string_list(item.get("spoiler_terms"), max_items=8)
    inferred_terms = _spoiler_terms_from_text(" ".join([detail, snippet]))
    seen_terms = {term.lower() for term in explicit_terms}
    terms = explicit_terms + [term for term in inferred_terms if term.lower() not in seen_terms]
    risk = _normalize_spoiler_risk(item.get("spoiler_risk"), terms=terms)
    public_detail = str(item.get("public_detail") or "").strip()
    if not public_detail:
        public_detail = _redact_spoiler_terms(detail, terms)
    else:
        public_detail = _redact_spoiler_terms(public_detail, terms)
    return public_detail[:220], risk, terms[:8]


def normalize_map_payload(
    payload: dict[str, Any],
    *,
    chunk_no: int,
    review_ids: list[int],
) -> dict[str, Any]:
    allowed_ids = set(review_ids)
    normalized_ids = _int_list(payload.get("review_ids"), allowed_ids) or list(review_ids)
    if not normalized_ids:
        raise ValueError("map payload review_ids is empty")

    aspects: dict[str, dict[str, Any]] = {}
    raw_aspects = payload.get("aspects", {})
    if isinstance(raw_aspects, dict):
        for key, value in raw_aspects.items():
            aspect = str(key).strip().lower()
            if aspect not in ALLOWED_ASPECTS or not isinstance(value, dict):
                continue
            evidence_ids = _int_list(value.get("evidence_ids"), set(normalized_ids))
            aspects[aspect] = {
                "pros": _string_list(value.get("pros")),
                "cons": _string_list(value.get("cons")),
                "evidence_ids": evidence_ids,
            }

    evidence_items: list[dict[str, Any]] = []
    raw_evidence = payload.get("evidence_items", [])
    if isinstance(raw_evidence, list):
        for item in raw_evidence[:24]:
            if not isinstance(item, dict):
                continue
            try:
                review_id = int(item.get("review_id"))
            except (TypeError, ValueError):
                continue
            if review_id not in normalized_ids:
                continue
            aspect = str(item.get("aspect", "")).strip().lower()
            if aspect not in ALLOWED_ASPECTS:
                continue
            detail = str(item.get("detail", "")).strip()
            snippet = str(item.get("snippet", "")).strip()
            if len(detail) < 12 or len(snippet) < 12:
                continue
            corrected = _prefer_source_language_detail(detail, snippet)
            if corrected != detail:
                # 번역된 detail/public_detail은 폐기하고 원문 snippet에서 재생성
                detail = corrected
                item = {**item, "public_detail": ""}
            public_detail, spoiler_risk, spoiler_terms = _public_detail_from_item(item, detail, snippet)
            evidence_items.append(
                {
                    "review_id": review_id,
                    "source": _normalize_source(item.get("source")),
                    "aspect": aspect,
                    "polarity": _normalize_polarity(item.get("polarity")),
                    "detail": detail[:220],
                    "public_detail": public_detail,
                    "spoiler_risk": spoiler_risk,
                    "spoiler_terms": spoiler_terms,
                    "snippet": snippet[:320],
                }
            )

    if not evidence_items:
        raise ValueError("map payload evidence_items is empty")

    quote_candidates: list[dict[str, Any]] = []
    raw_quotes = payload.get("quote_candidates", [])
    if isinstance(raw_quotes, list):
        for item in raw_quotes[:12]:
            if not isinstance(item, dict):
                continue
            try:
                review_id = int(item.get("review_id"))
            except (TypeError, ValueError):
                continue
            if review_id not in normalized_ids:
                continue
            snippet = str(item.get("snippet", "")).strip()
            if len(snippet) < 12:
                continue
            aspect = str(item.get("aspect", "")).strip().lower()
            quote_candidates.append(
                {
                    "review_id": review_id,
                    "polarity": _normalize_polarity(item.get("polarity")),
                    "aspect": aspect if aspect in ALLOWED_ASPECTS else "content",
                    "snippet": snippet[:320],
                }
            )

    source_mix = payload.get("source_mix")
    if not isinstance(source_mix, dict):
        source_mix = {}

    sentiment = payload.get("sentiment")
    if not isinstance(sentiment, dict):
        sentiment = {}

    return {
        "chunk_no": int(payload.get("chunk_no") or chunk_no),
        "review_ids": normalized_ids,
        "source_mix": {
            "steam_user": int(source_mix.get("steam_user", 0) or 0),
            "metacritic_user": int(source_mix.get("metacritic_user", 0) or 0),
            "metacritic_critic": int(source_mix.get("metacritic_critic", 0) or 0),
        },
        "sentiment": {
            "positive": int(sentiment.get("positive", 0) or 0),
            "mixed": int(sentiment.get("mixed", 0) or 0),
            "negative": int(sentiment.get("negative", 0) or 0),
        },
        "aspects": aspects,
        "playtime_signals": {
            "early": _string_list((payload.get("playtime_signals") or {}).get("early") if isinstance(payload.get("playtime_signals"), dict) else []),
            "mid": _string_list((payload.get("playtime_signals") or {}).get("mid") if isinstance(payload.get("playtime_signals"), dict) else []),
            "late": _string_list((payload.get("playtime_signals") or {}).get("late") if isinstance(payload.get("playtime_signals"), dict) else []),
        },
        "critic_signals": {
            "praise": _string_list((payload.get("critic_signals") or {}).get("praise") if isinstance(payload.get("critic_signals"), dict) else []),
            "criticism": _string_list((payload.get("critic_signals") or {}).get("criticism") if isinstance(payload.get("critic_signals"), dict) else []),
            "evidence_ids": _int_list((payload.get("critic_signals") or {}).get("evidence_ids") if isinstance(payload.get("critic_signals"), dict) else [], set(normalized_ids)),
        },
        "quote_candidates": quote_candidates,
        "evidence_items": evidence_items,
        "warnings": _string_list(payload.get("warnings")),
    }


def normalize_map_text(text: str, *, chunk_no: int, review_ids: list[int]) -> dict[str, Any]:
    return normalize_map_payload(
        safe_parse_json_object(text),
        chunk_no=chunk_no,
        review_ids=review_ids,
    )


def dumps_map_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _guess_aspect(text: str) -> str:
    lower = text.lower()
    checks = [
        ("optimization", ("프레임", "렉", "버그", "crash", "fps", "최적화")),
        ("controls", ("조작", "키보드", "패드", "마우스")),
        ("graphics", ("그래픽", "비주얼", "visual", "graphic")),
        ("price_value", ("가격", "가성비", "할인")),
        ("sound", ("사운드", "음악", "음향", "효과음", "bgm", "ost", "soundtrack", "sound design", "audio")),
        ("difficulty", ("난이도", "어렵", "쉽", "보스", "도전", "빡세", "souls", "difficulty", "challenging")),
        ("content", ("스토리", "이야기", "서사", "세계관", "설정", "분위기", "캐릭터", "story", "narrative", "plot", "writing", "characters", "lore", "worldbuilding", "world building")),
        ("gameplay", ("재미", "재밌", "노잼", "지루", "갓겜", "꿀잼", "게임성", "할맛", "fun", "gameplay", "addictive")),
    ]
    for aspect, keywords in checks:
        if any(keyword in lower for keyword in keywords):
            return aspect
    return "content"


def _guess_polarity(text: str) -> str:
    lower = text.lower()
    negative = ("싫", "별로", "비판", "반복", "짜증", "부술", "어렵", "불쾌", "렉", "버그", "아쉽")
    positive = ("좋", "재밌", "갓겜", "훌륭", "뛰어나", "추천", "몰입", "긴박")
    neg_count = sum(1 for keyword in negative if keyword in lower)
    pos_count = sum(1 for keyword in positive if keyword in lower)
    if pos_count > neg_count:
        return "positive"
    if neg_count > pos_count:
        return "negative"
    return "mixed"


def _review_lines_from_chunk(text: str) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    pattern = re.compile(r"\[review_id=(\d+)[^\]]*\]\s*(.*?)(?=\n\[review_id=|\Z)", re.S)
    for match in pattern.finditer(text or ""):
        review_id = int(match.group(1))
        body = " ".join(match.group(2).split())
        if body:
            rows.append((review_id, body))
    return rows


def legacy_text_to_map_payload(text: str, *, chunk_no: int, review_ids: list[int]) -> dict[str, Any]:
    source = "steam_user"
    evidence_items = []
    for review_id, body in _review_lines_from_chunk(text)[:8]:
        if review_id not in set(review_ids):
            continue
        detail = body[:180]
        snippet = body[:180]
        terms = _spoiler_terms_from_text(body)
        evidence_items.append(
            {
                "review_id": review_id,
                "source": source,
                "aspect": _guess_aspect(body),
                "polarity": _guess_polarity(body),
                "detail": detail,
                "public_detail": _redact_spoiler_terms(detail, terms),
                "spoiler_risk": "medium" if terms else "none",
                "spoiler_terms": terms[:8],
                "snippet": snippet,
            }
        )
    if not evidence_items and review_ids:
        detail = " ".join((text or "").split())[:180]
        if detail:
            terms = _spoiler_terms_from_text(detail)
            evidence_items.append(
                {
                    "review_id": review_ids[0],
                    "source": source,
                    "aspect": _guess_aspect(detail),
                    "polarity": _guess_polarity(detail),
                    "detail": detail,
                    "public_detail": _redact_spoiler_terms(detail, terms),
                    "spoiler_risk": "medium" if terms else "none",
                    "spoiler_terms": terms[:8],
                    "snippet": detail[:180],
                }
            )
    return {
        "chunk_no": chunk_no,
        "review_ids": list(review_ids),
        "source_mix": {"steam_user": len(review_ids), "metacritic_user": 0, "metacritic_critic": 0},
        "sentiment": {"positive": 0, "mixed": len(review_ids), "negative": 0},
        "aspects": {},
        "playtime_signals": {"early": [], "mid": [], "late": []},
        "critic_signals": {"praise": [], "criticism": [], "evidence_ids": []},
        "quote_candidates": [],
        "evidence_items": evidence_items,
        "warnings": ["legacy_map_adapter"],
    }


def _candidate_source_by_review_id(candidate_payload: dict[str, Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    for item in candidate_payload.get("evidence_items", []):
        if not isinstance(item, dict):
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        result[review_id] = _normalize_source(item.get("source"))
    return result


def _candidate_evidence_by_review_id(candidate_payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for item in candidate_payload.get("evidence_items", []):
        if not isinstance(item, dict):
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        result[review_id] = item
    return result


def _candidate_evidence_order(candidate_payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [item for item in candidate_payload.get("evidence_items", []) if isinstance(item, dict)]


def _coerce_llm_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_items: list[Any] = []
    if isinstance(payload.get("reviews"), list):
        raw_items.extend(payload["reviews"])
    if isinstance(payload.get("items"), list):
        raw_items.extend(payload["items"])
    if isinstance(payload.get("review_id"), (int, str)):
        raw_items.append(payload)
    if not raw_items and isinstance(payload.get("review_ids"), list):
        raw_items.extend({"review_id": review_id} for review_id in payload["review_ids"])
    return [item for item in raw_items if isinstance(item, dict)]


def _is_grounded_in_candidate(text: str, candidate_item: dict[str, Any]) -> bool:
    normalized = " ".join(str(text or "").lower().split())
    if len(normalized) < 12:
        return False
    candidate_text = " ".join(
        str(candidate_item.get(key, "") or "")
        for key in ("detail", "public_detail", "snippet")
    )
    candidate_normalized = " ".join(candidate_text.lower().split())
    if not candidate_normalized:
        return False
    return normalized in candidate_normalized or candidate_normalized[:80] in normalized


def ground_payload_with_candidate(
    payload: dict[str, Any],
    *,
    candidate_payload: dict[str, Any],
    chunk_no: int,
    review_ids: list[int],
) -> tuple[dict[str, Any], bool]:
    candidate_by_id = _candidate_evidence_by_review_id(candidate_payload)
    grounded = dict(payload)
    evidence_items: list[dict[str, Any]] = []
    changed = False
    seen: set[tuple[int, str]] = set()
    for item in payload.get("evidence_items", []):
        if not isinstance(item, dict):
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        candidate_item = candidate_by_id.get(review_id)
        if not candidate_item:
            continue
        snippet_grounded = _is_grounded_in_candidate(str(item.get("snippet", "")), candidate_item)
        if not snippet_grounded:
            replacement = dict(candidate_item)
            replacement["aspect"] = item.get("aspect") if str(item.get("aspect", "")).lower() in ALLOWED_ASPECTS else candidate_item.get("aspect")
            replacement["polarity"] = _normalize_polarity(item.get("polarity", candidate_item.get("polarity")))
            item = replacement
            changed = True
        key = (review_id, str(item.get("aspect", "")))
        if key in seen:
            changed = True
            continue
        seen.add(key)
        evidence_items.append(item)
    if not evidence_items:
        raise ValueError("grounded map payload has no usable evidence")
    grounded["evidence_items"] = evidence_items
    if changed:
        grounded["warnings"] = _string_list(payload.get("warnings")) + ["llm_grounding_repaired"]
    return normalize_map_payload(grounded, chunk_no=chunk_no, review_ids=review_ids), changed


def repair_llm_payload_with_candidate(
    payload: dict[str, Any],
    *,
    candidate_payload: dict[str, Any],
    chunk_no: int,
    review_ids: list[int],
) -> dict[str, Any]:
    allowed_ids = set(review_ids)
    sources = _candidate_source_by_review_id(candidate_payload)
    candidate_by_id = _candidate_evidence_by_review_id(candidate_payload)
    candidate_order = _candidate_evidence_order(candidate_payload)
    evidence_items: list[dict[str, Any]] = []
    for idx, item in enumerate(_coerce_llm_items(payload)[:8]):
        raw_id = item.get("review_id", item.get("id", item.get("reviewer_id")))
        if raw_id is None and idx < len(candidate_order):
            raw_id = candidate_order[idx].get("review_id")
        try:
            review_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if review_id not in allowed_ids:
            continue
        candidate_item = candidate_by_id.get(review_id, {})
        detail = str(
            item.get("detail")
            or item.get("content")
            or item.get("summary")
            or item.get("text")
            or item.get("snippet")
            or candidate_item.get("detail")
            or ""
        ).strip()
        snippet = str(
            item.get("snippet")
            or item.get("content")
            or candidate_item.get("snippet")
            or detail
        ).strip()
        if len(detail) < 12 or len(snippet) < 12:
            continue
        corrected = _prefer_source_language_detail(detail, snippet)
        translated = corrected != detail
        if translated:
            detail = corrected
        # 번역된 detail이면 LLM이 만든 public_detail도 신뢰할 수 없으므로 폐기 → 재생성
        public_detail = "" if translated else str(item.get("public_detail") or candidate_item.get("public_detail") or "").strip()
        spoiler_terms = _string_list(item.get("spoiler_terms"), max_items=8) or _string_list(candidate_item.get("spoiler_terms"), max_items=8)
        if not spoiler_terms:
            spoiler_terms = _spoiler_terms_from_text(" ".join([detail, snippet]))
        spoiler_risk = _normalize_spoiler_risk(
            item.get("spoiler_risk", candidate_item.get("spoiler_risk")),
            terms=spoiler_terms,
        )
        if not public_detail:
            public_detail = candidate_item.get("public_detail") or detail
        public_detail = _redact_spoiler_terms(str(public_detail), spoiler_terms)
        aspect = str(item.get("aspect", "")).strip().lower()
        if aspect not in ALLOWED_ASPECTS:
            aspect = _guess_aspect(detail)
        polarity = item.get("polarity", item.get("sentiment"))
        evidence_items.append(
            {
                "review_id": review_id,
                "source": sources.get(review_id, "steam_user"),
                "aspect": aspect,
                "polarity": _normalize_polarity(polarity) if polarity else _guess_polarity(detail),
                "detail": detail[:220],
                "public_detail": public_detail[:220],
                "spoiler_risk": spoiler_risk,
                "spoiler_terms": spoiler_terms[:8],
                "snippet": snippet[:320],
            }
        )
    if not evidence_items:
        raise ValueError("repairable LLM payload has no usable evidence")

    repaired = dict(candidate_payload)
    repaired["chunk_no"] = int(payload.get("chunk_no") or chunk_no)
    repaired["review_ids"] = list(review_ids)
    repaired["evidence_items"] = evidence_items
    repaired["warnings"] = _string_list(candidate_payload.get("warnings")) + ["llm_schema_repaired"]
    return normalize_map_payload(repaired, chunk_no=chunk_no, review_ids=review_ids)


def normalize_map_text_with_candidate(
    text: str,
    *,
    chunk_no: int,
    review_ids: list[int],
    candidate_payload: dict[str, Any],
) -> tuple[dict[str, Any], bool]:
    parsed = safe_parse_json_object(text)
    try:
        normalized = normalize_map_payload(parsed, chunk_no=chunk_no, review_ids=review_ids)
        return ground_payload_with_candidate(
            normalized,
            candidate_payload=candidate_payload,
            chunk_no=chunk_no,
            review_ids=review_ids,
        )
    except Exception:
        return (
            repair_llm_payload_with_candidate(
                parsed,
                candidate_payload=candidate_payload,
                chunk_no=chunk_no,
                review_ids=review_ids,
            ),
            True,
        )


def repair_llm_text_with_candidate_ids(
    text: str,
    *,
    candidate_payload: dict[str, Any],
    chunk_no: int,
    review_ids: list[int],
) -> dict[str, Any]:
    allowed_ids = set(review_ids)
    candidate_by_id = _candidate_evidence_by_review_id(candidate_payload)
    found_ids: list[int] = []
    for match in re.finditer(r'"(?:review_id|id|reviewer_id)"\s*:\s*"?(\d+)"?', text or ""):
        review_id = int(match.group(1))
        if review_id in allowed_ids and review_id in candidate_by_id and review_id not in found_ids:
            found_ids.append(review_id)
    if not found_ids:
        raise ValueError("malformed LLM text has no repairable review_ids")

    repaired = dict(candidate_payload)
    repaired["chunk_no"] = chunk_no
    repaired["review_ids"] = list(review_ids)
    repaired["evidence_items"] = [candidate_by_id[review_id] for review_id in found_ids[:8]]
    repaired["warnings"] = _string_list(candidate_payload.get("warnings")) + ["llm_truncated_json_repaired"]
    return normalize_map_payload(repaired, chunk_no=chunk_no, review_ids=review_ids)
