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
    "difficulty",
    "multiplayer",
    "bugs",
}


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
            evidence_items.append(
                {
                    "review_id": review_id,
                    "source": _normalize_source(item.get("source")),
                    "aspect": aspect,
                    "polarity": _normalize_polarity(item.get("polarity")),
                    "detail": detail[:220],
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
        ("sound", ("bgm", "ost", "음악", "사운드")),
        ("difficulty", ("난이도", "어렵", "보스", "말레니아", "트리가드")),
        ("optimization", ("프레임", "렉", "버그", "crash", "fps", "최적화")),
        ("controls", ("조작", "키보드", "패드", "마우스")),
        ("graphics", ("그래픽", "비주얼", "visual", "graphic")),
        ("price_value", ("가격", "가성비", "할인")),
        ("multiplayer", ("멀티", "친구", "코옵", "pvp")),
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
        snippet = body[:320]
        evidence_items.append(
            {
                "review_id": review_id,
                "source": source,
                "aspect": _guess_aspect(body),
                "polarity": _guess_polarity(body),
                "detail": body[:220],
                "snippet": snippet,
            }
        )
    if not evidence_items and review_ids:
        detail = " ".join((text or "").split())[:220]
        if detail:
            evidence_items.append(
                {
                    "review_id": review_ids[0],
                    "source": source,
                    "aspect": _guess_aspect(detail),
                    "polarity": _guess_polarity(detail),
                    "detail": detail,
                    "snippet": detail[:320],
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
    return [item for item in raw_items if isinstance(item, dict)]


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
        raw_id = item.get("review_id", item.get("id"))
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
        return normalize_map_payload(parsed, chunk_no=chunk_no, review_ids=review_ids), False
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
    for match in re.finditer(r'"(?:review_id|id)"\s*:\s*"?(\d+)"?', text or ""):
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
