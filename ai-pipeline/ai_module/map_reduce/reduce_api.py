from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential

from ai_module.map_reduce.map_schema import SPOILER_TERM_PATTERNS, _redact_spoiler_terms


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

GROUNDING_TERMS = (
    "강제종료",
    "데이터",
    "삭제",
    "버그",
    "길찾",
    "길 찾",
    "보스전",
    "불거인",
    "자유도",
    "소울",
    "빌드",
    "클리어",
    "입문",
    "림그레이브",
    "10시간",
    "그래픽",
    "사운드",
    "BGM",
    "전투",
    "출시",
    "후속작",
    "NPC",
    "퀘스트",
    "안티치트",
    "카지노",
    "최적화",
    "스토리",
)

NEGATIVE_DETAIL_TERMS = (
    "강제종료",
    "불합리",
    "열받",
    "비추천",
    "접음",
    "부술",
    "버그",
    "오류",
    "길찾",
    "길 찾",
    "3시간",
    "안됨",
    "안 됨",
    "진행이 안",
    "고통",
    "문제",
    "못해",
    "불만",
    "기다",
    "기달",
    "언제",
    "출시",
    "호소",
    "싫",
    "키기 싫",
    "불친절",
    "불편",
    "어렵",
    "어려움",
    "힘들",
    "좌절",
    "스트레스",
    "재미없",
    "재미없는",
)

POSITIVE_DETAIL_TERMS = (
    "재밌",
    "재미",
    "좋",
    "추천",
    "가능",
    "잘되어",
    "현실성",
    "즐",
    "시간 가는줄",
    "굉장",
    "명작",
    "인생게임",
    "입문",
    "클리어",
    "자유도",
)


ASPECT_KEY_MAP = {
    "그래픽": "graphics", "비주얼": "graphics", "graphics": "graphics", "visual": "graphics",
    "조작": "controls", "조작감": "controls", "controls": "controls", "control": "controls",
    "최적화": "optimization", "성능": "optimization", "optimization": "optimization", "performance": "optimization",
    "콘텐츠": "content", "스토리": "content", "content": "content", "story": "content",
    "가격": "price_value", "가성비": "price_value", "value": "price_value", "price": "price_value",
}

ASPECT_LABELS = {
    "graphics": "그래픽",
    "visual": "그래픽",
    "controls": "조작",
    "control": "조작",
    "optimization": "최적화",
    "performance": "최적화",
    "content": "콘텐츠",
    "story": "콘텐츠",
    "difficulty": "난이도",
    "combat": "전투",
    "sound": "사운드",
    "music": "사운드",
    "multiplayer": "멀티플레이",
    "price_value": "가격",
    "bugs": "버그",
}


CandidateDecision = Literal["accept", "ambiguous", "reject"]


@dataclass(frozen=True, slots=True)
class SummaryRule:
    name: str
    priority: int
    polarity: str
    template: str
    any_terms: tuple[str, ...] = ()
    all_terms: tuple[str, ...] = ()
    none_terms: tuple[str, ...] = ()
    regex_patterns: tuple[str, ...] = ()
    genres: tuple[str, ...] = ()
    aspects: tuple[str, ...] = ()

    def matches(self, text: str, *, active_genres: tuple[str, ...] = ()) -> bool:
        if active_genres and self.genres and not set(active_genres).intersection(self.genres):
            return False
        haystack = text.lower()
        if self.any_terms and not any(term.lower() in haystack for term in self.any_terms):
            return False
        if self.all_terms and not all(term.lower() in haystack for term in self.all_terms):
            return False
        if self.none_terms and any(term.lower() in haystack for term in self.none_terms):
            return False
        if self.regex_patterns and not any(re.search(pattern, text, flags=re.I) for pattern in self.regex_patterns):
            return False
        return True


PUBLIC_LIST_REJECT_TERMS = (
    "안됨",
    "새끼",
    "있음",
    "게임임",
    "진짜",
    "어요",
    "더라구요",
    "가득함",
    "없음",
    "했어염",
    "고염",
    "사세염",
)


CANDIDATE_REJECT_TERMS = (
    "살빠",
    "기달",
    "기다릴듯",
    "기다릴 듯",
    "근대 이거",
    "근데 이거",
    "했어염",
    "고염",
    "사세염",
)


SUMMARY_RULES_PATH = Path(__file__).with_name("rules") / "summary_rules.json"


def _tuple_field(raw: Any, *, field_name: str, rule_name: str) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise ValueError(f"summary rule {rule_name!r} has invalid {field_name}")
    return tuple(item for item in raw if item)


def _load_summary_rules() -> dict[str, tuple[SummaryRule, ...]]:
    try:
        raw = json.loads(SUMMARY_RULES_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"failed to read summary rules: {SUMMARY_RULES_PATH}") from exc
    if not isinstance(raw, dict):
        raise ValueError("summary rules root must be an object")

    loaded: dict[str, tuple[SummaryRule, ...]] = {}
    for polarity in ("positive", "negative"):
        entries = raw.get(polarity, [])
        if not isinstance(entries, list):
            raise ValueError(f"summary rules {polarity!r} must be a list")
        rules: list[SummaryRule] = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"summary rule in {polarity!r} must be an object")
            name = str(entry.get("name") or "").strip()
            template = str(entry.get("template") or "").strip()
            if not name or not template:
                raise ValueError(f"summary rule in {polarity!r} requires name and template")
            rules.append(
                SummaryRule(
                    name=name,
                    priority=int(entry.get("priority", 1000)),
                    polarity=polarity,
                    template=template,
                    any_terms=_tuple_field(entry.get("any_terms"), field_name="any_terms", rule_name=name),
                    all_terms=_tuple_field(entry.get("all_terms"), field_name="all_terms", rule_name=name),
                    none_terms=_tuple_field(entry.get("none_terms"), field_name="none_terms", rule_name=name),
                    regex_patterns=_tuple_field(entry.get("regex_patterns"), field_name="regex_patterns", rule_name=name),
                    genres=_tuple_field(entry.get("genres"), field_name="genres", rule_name=name),
                    aspects=_tuple_field(entry.get("aspects"), field_name="aspects", rule_name=name),
                )
            )
        loaded[polarity] = tuple(sorted(rules, key=lambda rule: rule.priority))
    return loaded


SUMMARY_RULES = _load_summary_rules()
POSITIVE_SUMMARY_RULES = SUMMARY_RULES["positive"]
NEGATIVE_SUMMARY_RULES = SUMMARY_RULES["negative"]


@dataclass(slots=True)
class BucketSummary:
    summary: str
    sentiment_overall: str | None
    sentiment_score: float | None
    pros: list[str]
    cons: list[str]
    keywords: list[str]


@dataclass(slots=True)
class FinalSummary:
    # unified 요약
    one_liner: str
    aspect_scores: dict[str, Any]
    full_text: str = ""
    sentiment_overall: str | None = None
    sentiment_score: float | None = None
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    # Sprint 4: 플레이타임 버킷별 요약
    playtime_early: BucketSummary | None = None
    playtime_mid: BucketSummary | None = None
    playtime_late: BucketSummary | None = None
    # Sprint 4: 비평가 요약
    critic: BucketSummary | None = None
    # B안: user 전용 요약 (unified body 폐지 후 "유저 리뷰 요약" 섹션의 데이터원)
    user: BucketSummary | None = None
    # 메타
    input_tokens: int = 0
    output_tokens: int = 0
    reduce_usage: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    is_retryable: bool | None = None


REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only. No markdown, no code fences.

Each [map_N] input follows this structure:
PROS: bullet points of positive points
CONS: bullet points of negative points / issues
ASPECTS: aspects discussed (graphics / controls / optimization / content / price_value)
IDS: evidence review_ids
Use ASPECTS fields to determine which aspect_scores to populate — only score aspects that appear in at least one chunk's ASPECTS list.

Required top-level keys:
- unified: {
    one_liner: one sentence overall verdict (Korean),
    aspect_scores: {
      graphics: {label, score},
      controls: {label, score},
      optimization: {label, score},
      content: {label, score},
      price_value: {label, score}
    }  (scores 0.0–10.0 with anchors:
       2.0 = severe issues reported by majority,
       5.0 = mixed reception with notable complaints,
       7.0 = generally praised with minor flaws,
       9.0 = exceptional, widely acclaimed;
       label is a short Korean adjective),
    sentiment_overall: one of [positive, mixed, negative],
    sentiment_score: number in range 0..100,
    pros: [string] at least 3 items,
    cons: [string] at least 2 items,
    keywords: [string] 5–8 items
  }
- playtime: {
    early: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null,
    mid:   { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null,
    late:  { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  }
  (Each bucket: 2–3 sentences in Korean. null ONLY if input array is empty.)
- critic: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  (critic: based ONLY on critic reviews; do NOT compare with user opinion; label as "출시 당시 전문가 평가". summary 4–6 sentences in Korean with concrete praise, criticism, and evaluation criteria. null ONLY if critic input array is empty.)
- user: { summary, sentiment_overall, sentiment_score, pros, cons, keywords } | null
  (user: based ONLY on user-side map groups (i.e. all/early/mid/late, excluding critic); label as "유저 평가". summary 5–7 sentences in Korean covering concrete strengths, repeated complaints, recommended players, and caution cases. null ONLY if user input is empty.)

Rules:
- unified is based on the "all" group.
- playtime buckets are independent; mention sentiment trend across buckets naturally in each summary.
- critic is independent from user sentiment; never compare or mention divergence.
- If evidence is missing for an aspect, omit it rather than fabricate.
- If a segment input array is empty, return null for that segment.
- If [previous_summary] is provided, use it as baseline context. On any point where new map groups conflict with it, the new map evidence takes precedence. Do not reproduce prior sentences verbatim.
- sentiment_overall and sentiment_score MUST be consistent: positive → score >= 60, negative → score <= 45, mixed → 40 <= score <= 65.

Rules about repetition:
- Do NOT repeat the same sentence or phrase verbatim across different sections (unified, playtime buckets, critic).
- Ensure sentences are unique; when similar points are necessary across sections, paraphrase concisely.
""".strip()


def _safe_parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
    raw = re.sub(r"\n?```$", "", raw).strip()
    return json.loads(raw)


def _to_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _summary_text(value: Any) -> str:
    if isinstance(value, list):
        return " ".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _normalize_sentiment_overall(value: Any) -> str | None:
    text = str(value).strip().lower()
    if text in {"positive", "mixed", "negative"}:
        return text
    return None


def _normalize_sentiment_score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(0.0, min(100.0, score)), 2)


def _parse_bucket(data: Any) -> BucketSummary | None:
    if not isinstance(data, dict):
        return None
    summary_str = _summary_text(data.get("summary", ""))
    if not summary_str:
        return None
    return BucketSummary(
        summary=summary_str,
        sentiment_overall=_normalize_sentiment_overall(data.get("sentiment_overall")),
        sentiment_score=_normalize_sentiment_score(data.get("sentiment_score")),
        pros=_to_string_list(data.get("pros", [])),
        cons=_to_string_list(data.get("cons", [])),
        keywords=_to_string_list(data.get("keywords", [])),
    )


class ReduceParseError(ValueError):
    pass


def _is_valid_unified(unified: dict) -> bool:
    """unified 요약의 핵심 필드 유효성 + sentiment 일관성 검증."""
    if not (
        str(unified.get("one_liner", "")).strip()
        and len(_to_string_list(unified.get("pros", []))) >= 2
        and len(_to_string_list(unified.get("cons", []))) >= 1
        and _normalize_sentiment_overall(unified.get("sentiment_overall")) is not None
    ):
        return False

    overall = _normalize_sentiment_overall(unified.get("sentiment_overall"))
    score = _normalize_sentiment_score(unified.get("sentiment_score"))
    if overall is not None and score is not None:
        if overall == "positive" and score < 60:
            return False
        if overall == "negative" and score > 45:
            return False
    return True


def classify_reduce_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ReduceParseError):
        return ("parse_error", False)

    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return ("timeout", True)

    if "quota" in message or "rate limit" in message or "429" in message:
        return ("quota", False)

    return ("upstream_unavailable", True)


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)
async def _generate_reduce_response(
    client: AsyncGroq,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
):
    return await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        temperature=0.2,
    )


FEATURE_QUALITY_RULES = """
Quality rules:
- Avoid vague group claims such as "users praised combat" unless concrete evidence follows.
- Ground natural-language fields in evidence_items or representative quotes.
- Good sentences include at least three of: aspect, concrete situation, evaluation feeling, source.
- Do not invent Metacritic user review details from score anchors.
- Use score anchors only to calibrate tone and sentiment scores.
- In Korean output, prefer concrete review details over abstract category labels.
- Preserve spoiler-safe specificity: describe experience type, progression phase, failure condition, emotional effect, or technical symptom without exposing boss names, ending names, plot twists, deaths, or late-area names.
- Do not use filler such as "다양한 경험", "다양한 의견", "일부 사용자", or "어려울 수 있습니다" unless the same sentence names a concrete review condition and review_id.
- Do not write "리뷰어 N" or "reviewer N"; use only the exact "(review_id=N)" anchor from the supplied evidence.
- Bad Korean: "유저들은 난이도를 칭찬했다", "콘텐츠가 다양하다", "의견이 분분하다".
- Good Korean: "한 리뷰는 불의 거인에서 같은 공격에 계속 맞아 분노했다고 했고, 다른 리뷰는 길잡이가 불친절해도 그 자유도 덕분에 난이도를 우회할 수 있다고 봤다."
- Every summary must mention at least four concrete details from evidence_items unless fewer than four evidence items exist.
- pros and cons must be self-contained review-grounded sentences, not short labels.
- pros and cons must be 35-120 Korean characters, include a concrete reason or condition, and include review_id when available.
""".strip()


def _parse_map_payloads(items: list[str]) -> list[dict[str, Any]]:
    from ai_module.map_reduce.map_schema import legacy_text_to_map_payload, normalize_map_payload, safe_parse_json_object

    payloads: list[dict[str, Any]] = []
    for idx, item in enumerate(items, 1):
        try:
            parsed = safe_parse_json_object(item)
            review_ids = [int(v) for v in parsed.get("review_ids", []) if str(v).isdigit()]
            parsed = normalize_map_payload(parsed, chunk_no=int(parsed.get("chunk_no") or idx), review_ids=review_ids)
        except Exception:
            parsed = legacy_text_to_map_payload(item, chunk_no=idx, review_ids=[idx])
        payloads.append(parsed)
    return payloads


def _evidence_subset(payloads: list[dict[str, Any]], *, limit: int = 80) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[Any, str, str]] = set()
    for payload in payloads:
        evidence = payload.get("evidence_items", [])
        if not isinstance(evidence, list):
            continue
        for item in evidence:
            if isinstance(item, dict):
                spoiler_terms = [str(term) for term in item.get("spoiler_terms", []) if str(term).strip()] if isinstance(item.get("spoiler_terms"), list) else []
                spoiler_risk = str(item.get("spoiler_risk") or "none")
                raw_detail = str(item.get("public_detail") or item.get("detail", ""))
                detail = _redact_spoiler_terms(" ".join(raw_detail.split()), spoiler_terms)[:180]
                snippet = " ".join(str(item.get("snippet", "")).split())[:180]
                if spoiler_risk in {"medium", "high"}:
                    snippet = detail
                if len(detail) < 12 or len(snippet) < 12:
                    continue
                key = (item.get("review_id"), str(item.get("aspect", "")), detail.lower())
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    {
                        "review_id": item.get("review_id"),
                        "source": item.get("source"),
                        "aspect": item.get("aspect"),
                        "polarity": item.get("polarity"),
                        "detail": detail,
                        "snippet": snippet,
                        "spoiler_risk": spoiler_risk,
                    }
                )
    polarity_rank = {"negative": 0, "positive": 1, "mixed": 2}
    rows.sort(key=lambda item: (polarity_rank.get(str(item.get("polarity")), 3), str(item.get("aspect", "")), int(item.get("review_id") or 0)))
    return rows[:limit]


def _signal_subset(payloads: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for payload in payloads:
        value = payload.get(key)
        if isinstance(value, dict):
            result.append(value)
    return result


def _json_block(title: str, data: Any, max_chars: int = 12000) -> str:
    text = json.dumps(data, ensure_ascii=False)
    if len(text) > max_chars:
        text = text[:max_chars] + "...(truncated)"
    return f"[{title}]\n{text}"


def _build_feature_prompt(
    *,
    feature: str,
    language_code: str,
    payloads: list[dict[str, Any]],
    output_contract: dict[str, Any],
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    representative_quotes: list[str] | None = None,
    extra: dict[str, Any] | None = None,
    evidence_limit: int = 80,
) -> str:
    strict_requirements = {
        "summary_style": [
            "Write Korean natural-language fields as dense evidence synthesis.",
            "Do not start with generic genre framing unless followed by concrete review detail in the same sentence.",
            "Use spoiler-safe review-grounded details such as combat situation, progression phase, crash/cutscene symptom, control issue, repeated farming, playtime condition, or quoted feeling.",
            "Avoid unsupported generalizations like 'players have various experiences'.",
            "Avoid filler Korean such as '다양한 경험', '다양한 의견', '일부 사용자', '어려울 수 있습니다', and '다양한 콘텐츠'.",
            "Do not expose specific boss names, ending names, plot twists, character deaths, late-area names, or quest resolutions in public output.",
            "Do not write reviewer labels such as '리뷰어 9'. Use '(review_id=9)' only when the sentence is grounded in that exact evidence item.",
        ],
            "minimum_detail": {
            "summary": "At least 5 Korean sentences for user/final context when enough evidence exists; each sentence should include concrete evidence detail.",
            "pros": "Each item must be a complete 35-120 character Korean sentence with a concrete situation or quoted feeling from evidence and review_id.",
            "cons": "Each item must be a complete 35-120 character Korean sentence with a concrete failure mode, frustration condition, or quoted complaint and review_id.",
        },
        "grounding_format": "When natural, include review_id in parentheses like (review_id=12) so grounding can be audited.",
    }
    sections = [
        f"feature={feature}",
        f"language={language_code}",
        FEATURE_QUALITY_RULES,
        _json_block("strict_requirements", strict_requirements, 5000),
        _json_block("output_contract", output_contract, 4000),
        _json_block("evidence_items", _evidence_subset(payloads, limit=evidence_limit), 9000),
    ]
    if score_anchors:
        sections.append(_json_block("score_anchors", score_anchors, 3000))
    if category_frequency:
        sections.append(_json_block("category_frequency", category_frequency, 5000))
    if representative_quotes:
        sections.append(_json_block("representative_quotes", representative_quotes[:4], 2200))
    if extra:
        sections.append(_json_block("extra_context", extra, 4500))
    return "\n\n".join(sections)


async def _run_feature_json(
    *,
    client: AsyncGroq,
    model_name: str,
    feature: str,
    prompt: str,
    timeout_sec: int,
) -> tuple[dict[str, Any], dict[str, int]]:
    system_prompt = (
        "You are a game review synthesis engine. Return JSON only. "
        "Every natural-language field must be grounded in the supplied evidence. "
        "When language=ko, write detailed Korean sentences using concrete review evidence, not vague category summaries."
    )
    response = await asyncio.wait_for(
        _generate_reduce_response(client, model_name, system_prompt, prompt),
        timeout=timeout_sec,
    )
    raw_text = (response.choices[0].message.content or "").strip()
    parsed = _safe_parse_json(raw_text)
    usage = {
        "requests": 1,
        "input_tokens": int(response.usage.prompt_tokens or 0),
        "output_tokens": int(response.usage.completion_tokens or 0),
        "retry": 0,
    }
    logger.info(
        "feature reduce completed: feature=%s input_tokens=%d output_tokens=%d chars=%d",
        feature,
        usage["input_tokens"],
        usage["output_tokens"],
        len(raw_text),
    )
    return parsed, usage


def _bucket_to_dict(bucket: BucketSummary | None) -> dict[str, Any] | None:
    if bucket is None:
        return None
    return {
        "summary": bucket.summary,
        "sentiment_overall": bucket.sentiment_overall,
        "sentiment_score": bucket.sentiment_score,
        "pros": bucket.pros,
        "cons": bucket.cons,
        "keywords": bucket.keywords,
    }


def _parse_feature_bucket(data: Any) -> BucketSummary | None:
    return _parse_bucket(data)


def _all_spoiler_terms() -> list[str]:
    terms: list[str] = []
    for patterns in SPOILER_TERM_PATTERNS.values():
        for term in patterns:
            if term not in terms:
                terms.append(term)
    return terms


def _sanitize_public_text(text: str) -> str:
    sanitized = _redact_spoiler_terms(str(text or ""), _all_spoiler_terms())
    replacements = {
        "ㅈㄴ": "매우 ",
        "ㅈ같아서": "불합리하게 느껴져서",
        "ㅈ같": "불합리하게 느껴지",
        "존나": "매우 ",
        "개같": "거칠게 느껴지",
        "병1신": "문제가 많은",
        "병2신": "문제가 많은",
        "정2병": "비매너",
        "ㅅㅂ": "",
        "씨발": "",
        "갓겜임": "강한 만족감을 주는 게임임",
        "갓겜": "강한 만족감을 주는 게임",
        "끔찎": "끔찍",
        "ㅐ미": "재미",
        "꺨": "깰",
        "깰을": "깰",
        "난이도": "진행 장벽",
        "다양한 의견이 있지만, ": "",
        "다양한 의견": "상반된 반응",
        "다양한 경험": "상반된 플레이 경험",
        "다양한 콘텐츠": "탐험 콘텐츠",
        "어려울 수 있습니다": "진입 장벽이 있습니다",
        "일부 사용자": "한 리뷰는",
        "긍정적인 평가": "구체적인 호평",
        "부정적인 평가": "구체적인 불만",
        "대체로": "",
        "대부분의 사용자는": "근거 리뷰는",
        "대부분의 리뷰어들은": "근거 리뷰는",
        "대부분": "",
        "의견이 분분하지만": "긍정과 불만이 함께 나타나지만",
        "의견도 분분": "긍정과 불만이 함께 나타남",
        "의견이 분분": "긍정과 불만이 함께 나타남",
        "전반적인 품질": "구체적인 플레이 경험",
        "많은 사용자들이": "여러 리뷰는",
        "많은 플레이어들이": "여러 리뷰는",
        "많은 리뷰어": "여러 리뷰는",
        "일부 리뷰어": "한 리뷰는",
        "일부 플레이어": "한 리뷰는",
        "근거 리뷰어들은": "여러 리뷰는",
        "근거 리뷰어는": "한 리뷰는",
        "근거 리뷰어": "한 리뷰는",
        "review_id 미제공": "근거 ID 없음",
        "review_id=미제공": "근거 ID 없음",
        "대표적인 따옴문": "근거 ID 없음",
        "한 리뷰는는": "한 리뷰는",
        "사용자들에게": "리뷰에서",
    }
    for src, dst in replacements.items():
        sanitized = sanitized.replace(src, dst)
    sanitized = sanitized.replace("호평를", "호평을").replace("불만를", "불만을")
    sanitized = (
        sanitized.replace("진행 장벽는", "진행 장벽은")
        .replace("진행 장벽를", "진행 장벽을")
        .replace("진행 장벽가", "진행 장벽이")
        .replace("진행 장벽와", "진행 장벽과")
    )
    sanitized = sanitized.replace("문제가 많은같은", "문제가 많은")
    sanitized = sanitized.replace("@", " ")
    sanitized = re.sub(r"(?:리뷰어|reviewer)\s*(\d+)", r"(review_id=\1)", sanitized, flags=re.I)
    return " ".join(sanitized.split())


def _evidence_text_index(payloads: list[dict[str, Any]]) -> dict[int, str]:
    index: dict[int, str] = {}
    for item in _evidence_subset(payloads, limit=200):
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        text = " ".join(str(item.get(key, "") or "") for key in ("detail", "public_detail", "snippet"))
        index[review_id] = " ".join([index.get(review_id, ""), text]).lower()
    return index


def _repair_review_id_anchors(text: str, evidence_index: dict[int, str] | None) -> str:
    return text


def _is_vague_public_sentence(text: str) -> bool:
    return any(
        pattern in text
        for pattern in (
            "다양한 사용자",
            "다양한 플레이어",
            "높은 평가",
            "대체로",
            "대부분",
            "일부 사례",
            "전반적인 품질",
            "많은 리뷰어",
            "많은 플레이어",
            "플레이어들은",
            "유저들은",
            "일부 리뷰어",
            "일부 플레이어",
            "사용자들에게",
            "다양한 활동",
            "게임의 멀티플레이 기능",
            "의 플레이어들",
            "긍정적인 평가",
            "부정적인 평가",
            "의견이 분분",
            "의견도 분분",
            "긍정과 불만이 함께",
            "상반된 플레이 경험",
            "근거 리뷰는",
            "근거 리뷰어",
            "근거 플레이어",
            "측면의",
            "경험이 핵심",
            "진행 장벽는",
            "진행 장벽를",
            "진행 장벽가",
            "진행 장벽와",
        )
    )


def _segment_anchor_failures(segment: str, evidence_index: dict[int, str] | None) -> list[str]:
    if not evidence_index:
        return []
    reviewer_refs = {int(match.group(1)) for match in re.finditer(r"(?:리뷰어|reviewer)\s*(\d+)", segment, flags=re.I)}
    anchor_refs = {int(match.group(1)) for match in re.finditer(r"review_id\s*=\s*(\d+)", segment)}
    if reviewer_refs and anchor_refs and not reviewer_refs.issubset(anchor_refs):
        return ["reviewer_label_mismatch"]
    terms = [term for term in GROUNDING_TERMS if term.lower() in segment.lower()]
    if not terms or not anchor_refs:
        return []
    failures = []
    for term in terms:
        normalized_term = re.sub(r"\s+", "", term.lower())
        if not any(
            term.lower() in str(evidence_index.get(review_id, ""))
            or normalized_term in re.sub(r"\s+", "", str(evidence_index.get(review_id, "")).lower())
            for review_id in anchor_refs
        ):
            failures.append(term)
    return failures


def _sanitize_grounded_text(text: Any, evidence_index: dict[int, str] | None = None) -> str:
    sanitized = _repair_review_id_anchors(_sanitize_public_text(str(text or "")), evidence_index)
    if not evidence_index:
        return sanitized
    segments = [part.strip() for part in re.split(r"(?<=[.!?。])\s+", sanitized) if part.strip()]
    if not segments:
        return sanitized
    kept: list[str] = []
    for segment in segments:
        if _is_vague_public_sentence(segment):
            continue
        if _segment_anchor_failures(segment, evidence_index):
            continue
        kept.append(segment)
    return " ".join(kept) if kept else ""


def _normalize_public_sentence_anchor(text: str) -> str:
    normalized = " ".join(str(text or "").split()).strip()
    if not normalized:
        return ""

    prefix = re.match(r"^\(?\s*review_id\s*=\s*(\d+)\s*\)?\s*[:：,-]?\s*(.+)$", normalized, flags=re.I)
    if prefix:
        review_id, body = prefix.group(1), prefix.group(2).strip()
        body = re.sub(r"\s*\(?\s*review_id\s*=\s*\d+\s*\)?\s*\.?$", "", body, flags=re.I).strip()
        normalized = f"{body} (review_id={review_id})"

    mid = re.match(r"^(.+?)\s*\(?\s*review_id\s*=\s*(\d+)\s*\)?\s+(.+)$", normalized, flags=re.I)
    if mid:
        lead, review_id, tail = mid.group(1).strip(), mid.group(2), mid.group(3).strip()
        tail = re.sub(r"\s*\(?\s*review_id\s*=\s*\d+\s*\)?\s*\.?$", "", tail, flags=re.I).strip()
        body = tail if len(tail) >= 16 else f"{lead} {tail}".strip()
        normalized = f"{body} (review_id={review_id})"

    normalized = re.sub(r"\s*\.\s*\(review_id=", " (review_id=", normalized)
    normalized = normalized.rstrip(" .")
    return f"{normalized}."


def _review_id_count(text: str) -> int:
    return len(re.findall(r"review_id\s*=\s*\d+", str(text or "")))


def _fallback_items_from_evidence(
    evidence_items: list[dict[str, Any]],
    *,
    polarity: str,
    existing: list[str],
    limit: int,
) -> list[str]:
    result = list(existing)
    seen_ids = {int(match.group(1)) for text in result for match in re.finditer(r"review_id\s*=\s*(\d+)", text)}
    for item in evidence_items:
        if str(item.get("polarity")) != polarity:
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        if review_id in seen_ids:
            continue
        detail = _sanitize_public_text(str(item.get("detail") or item.get("snippet") or ""))
        if len(detail) < 18 or len(detail) > 170 or re.search(r"([가-힣A-Za-z])\1{5,}", detail):
            continue
        sentence = f"{detail}라는 실제 리뷰 근거가 있습니다 (review_id={review_id})."
        if 35 <= len(sentence) <= 260:
            result.append(sentence)
            seen_ids.add(review_id)
        if len(result) >= limit:
            break
    return result


def _sanitize_public_list(values: Any, evidence_index: dict[int, str] | None = None) -> list[str]:
    result: list[str] = []
    for item in _to_string_list(values):
        sanitized = _normalize_public_sentence_anchor(_sanitize_grounded_text(item, evidence_index))
        if len(sanitized) < 35 or not re.search(r"review_id\s*=\s*\d+", sanitized):
            continue
        if _review_id_count(sanitized) != 1:
            continue
        if len(sanitized) > 180:
            continue
        if any(term in sanitized for term in PUBLIC_LIST_REJECT_TERMS):
            continue
        if _is_low_quality_detail(sanitized):
            continue
        if re.search(r"([가-힣A-Za-z])\1{5,}", sanitized):
            continue
        if _segment_anchor_failures(sanitized, evidence_index):
            continue
        result.append(sanitized)
    return result


def _compact_detail_for_sentence(detail: str) -> str:
    cleaned = _sanitize_public_text(detail)
    cleaned = cleaned.replace("호평를", "호평을").replace("불만를", "불만을")
    cleaned = re.sub(r"(?i)^리뷰에서는\s*['\"]?", "", cleaned)
    cleaned = re.sub(r"['\"]?\s*라고\s*(?:표현|언급).*?$", "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"^[,.\s]+", "", cleaned)
    cleaned = re.sub(r"[.。]+$", "", cleaned)
    parts = [part.strip(" '\"") for part in re.split(r"(?<=[.!?。])\s+|[,;]", cleaned) if part.strip(" '\"")]
    candidates = [part for part in parts if len(part) >= 16]
    if candidates:
        keywords = GROUNDING_TERMS + NEGATIVE_DETAIL_TERMS + ("재밌", "재미", "불편", "기다", "친구", "탐험", "현실성")
        scored = sorted(
            candidates,
            key=lambda part: (any(term.lower() in part.lower() for term in keywords), min(len(part), 80)),
            reverse=True,
        )
        cleaned = scored[0]
    if len(cleaned) > 72:
        cleaned = cleaned[:72].rstrip() + "..."
    return cleaned


def _positive_clause(detail: str) -> str:
    cleaned = _sanitize_public_text(detail)
    clauses = [
        part.strip(" .'\"")
        for part in re.split(r"하지만|그러나|그대신|그 대신|근데|근대|지만|[,.;]", cleaned)
        if part.strip(" .'\"")
    ]
    snippets = re.findall(r"[^.!?。]{0,28}(?:재밌|재미|좋|추천|가능|잘되어|현실성|즐|굉장|명작|인생게임|입문|클리어|자유도)[^.!?。]{0,36}", cleaned)
    clauses.extend(part.strip(" .'\"") for part in snippets if part.strip(" .'\""))
    for clause in clauses:
        if len(clause) < 10:
            continue
        if "던가" in clause or "굳이" in clause or "열받" in clause or ("라도" in clause and "재밌" in clause):
            continue
        if any(term in clause for term in POSITIVE_DETAIL_TERMS) and not any(term in clause for term in NEGATIVE_DETAIL_TERMS):
            return clause
    return ""


def _is_low_quality_detail(detail: str) -> bool:
    normalized = " ".join(str(detail or "").split())
    if len(normalized) < 8:
        return True
    if any(term in normalized for term in CANDIDATE_REJECT_TERMS):
        return True
    if "이 앞" in normalized or "있으라" in normalized or "매우매우" in normalized:
        return True
    if "던가" in normalized or "굳이" in normalized or "열받" in normalized:
        return True
    if re.search(r"[A-Za-z]{8,}", normalized) and not re.search(r"[가-힣]", normalized):
        return True
    if re.search(r"[A-Za-z]{10,}[;:]", normalized):
        return True
    if re.search(r"^[a-zA-Z;:'\"\\/\s]+$", normalized):
        return True
    return False


def _candidate_quality_decision(detail: str) -> CandidateDecision:
    normalized = " ".join(str(detail or "").split())
    if _is_low_quality_detail(normalized):
        return "reject"
    if len(normalized) < 18:
        return "ambiguous"
    has_grounding = any(term.lower() in normalized.lower() for term in GROUNDING_TERMS)
    has_sentiment = any(term in normalized for term in POSITIVE_DETAIL_TERMS + NEGATIVE_DETAIL_TERMS)
    if has_grounding and has_sentiment:
        return "accept"
    return "ambiguous"


def _classify_ambiguous_candidate(detail: str, *, polarity: str) -> bool:
    """Cheap local classifier hook; only ambiguous candidates reach here.

    A remote LLM can be plugged in later, but the default path stays deterministic
    to avoid extra tokens and cost during normal reduce execution.
    """
    if polarity == "positive":
        return any(term in detail for term in POSITIVE_DETAIL_TERMS) and not _has_negative_detail(detail)
    return _has_negative_detail(detail)


def _apply_summary_rules(detail: str, *, polarity: str) -> str:
    rules = POSITIVE_SUMMARY_RULES if polarity == "positive" else NEGATIVE_SUMMARY_RULES
    for rule in rules:
        if rule.matches(detail):
            return rule.template
    return ""


def _has_negative_detail(detail: str) -> bool:
    if "스트레스" in detail and "풀" in detail:
        detail = detail.replace("스트레스", "")
    detail = detail.replace("언제나", "")
    return any(term in detail for term in NEGATIVE_DETAIL_TERMS)


def _review_based_sentence(detail: str, *, polarity: str) -> str:
    raw_text = _sanitize_public_text(detail)
    text = _compact_detail_for_sentence(detail)
    decision = _candidate_quality_decision(text)
    if decision == "reject":
        rule_sentence = _apply_summary_rules(raw_text, polarity=polarity)
        return rule_sentence
    if decision == "ambiguous" and not _classify_ambiguous_candidate(raw_text, polarity=polarity):
        rule_sentence = _apply_summary_rules(raw_text, polarity=polarity)
        if not rule_sentence:
            return ""

    rule_sentence = _apply_summary_rules(raw_text, polarity=polarity)
    if rule_sentence:
        return rule_sentence

    if polarity == "positive":
        if "재밌" in text or "재미" in text:
            return f"{text}는 반응이 있습니다"
        if "좋" in text or "추천" in text:
            return f"{text}는 긍정 반응이 있습니다"
        return f"{text}는 장점으로 언급됐습니다"
    return ""


def _public_detail_for_sentence(item: dict[str, Any]) -> str:
    return str(item.get("public_detail") or item.get("detail") or item.get("snippet") or "")


def _aspect_label(item: dict[str, Any]) -> str:
    aspect = str(item.get("aspect") or "content").strip().lower()
    return ASPECT_LABELS.get(aspect, "리뷰")


def _sentence_subject(item: dict[str, Any], detail: str) -> str:
    label = _aspect_label(item)
    source = _public_detail_for_sentence(item)
    if label != "리뷰" and label.lower() in source.lower():
        return f"{label} 측면에서는"
    if label != "리뷰" and label in source:
        return f"{label} 측면에서는"
    if label == "콘텐츠":
        return "플레이 경험에서는"
    return "해당 리뷰에서는"


def _evidence_sentence(item: dict[str, Any], *, polarity: str) -> str | None:
    try:
        review_id = int(item.get("review_id"))
    except (TypeError, ValueError):
        return None
    detail = _compact_detail_for_sentence(str(item.get("detail") or item.get("snippet") or ""))
    if len(detail) < 10 or re.search(r"([가-힣A-Za-z])\1{5,}", detail):
        return None
    aspect = str(item.get("aspect") or "content")
    if polarity == "positive":
        if aspect in {"difficulty", "controls"}:
            body = f"전투나 조작 흐름에서 {detail}는 식의 호평이 확인됩니다"
        elif aspect == "sound":
            body = f"사운드 경험에서는 {detail}는 반응이 장점으로 남습니다"
        elif aspect == "graphics":
            body = f"비주얼 측면에서는 {detail}는 인상이 장점으로 언급됩니다"
        else:
            body = f"플레이 경험에서는 {detail}는 구체적인 호평이 확인됩니다"
    else:
        if "강제종료" in detail or "버그" in detail:
            body = f"기술 문제로는 {detail}는 불만이 확인됩니다"
        elif "길" in detail:
            body = f"진행 동선에서는 {detail}는 불편이 반복적으로 드러납니다"
        elif "난이도" in detail or "보스" in detail:
            body = f"난이도 측면에서는 {detail}는 부담으로 작용합니다"
        else:
            body = f"주의할 점으로는 {detail}는 비판이 확인됩니다"
    sentence = f"{body} (review_id={review_id})."
    if 35 <= len(sentence) <= 180:
        return sentence
    return None


def _fallback_compact_items_from_evidence(
    evidence_items: list[dict[str, Any]],
    *,
    polarities: tuple[str, ...],
    existing: list[str],
    limit: int,
) -> list[str]:
    result = list(existing)
    seen_ids = {int(match.group(1)) for text in result for match in re.finditer(r"review_id\s*=\s*(\d+)", text)}
    for item in evidence_items:
        if str(item.get("polarity")) not in polarities:
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        if review_id in seen_ids:
            continue
        detail = _sanitize_public_text(str(item.get("detail") or item.get("snippet") or ""))
        has_negative_detail = _has_negative_detail(detail)
        if polarities == ("positive",) and has_negative_detail:
            continue
        if polarities in {("negative",), ("mixed",)} and not has_negative_detail:
            continue
        if len(detail) < 18 or re.search(r"([가-힣A-Za-z])\1{5,}", detail):
            continue
        if len(detail) > 110:
            detail = detail[:110].rstrip() + "..."
        sentence = f"{detail}라는 반응이 있습니다 (review_id={review_id})."
        if 35 <= len(sentence) <= 180:
            result.append(sentence)
            seen_ids.add(review_id)
        if len(result) >= limit:
            break
    return result


def _drop_negative_items(values: list[str]) -> list[str]:
    return [item for item in values if not any(term in item for term in NEGATIVE_DETAIL_TERMS)]


def _evidence_sentence_v2(item: dict[str, Any], *, polarity: str, detail_override: str | None = None) -> str | None:
    try:
        review_id = int(item.get("review_id"))
    except (TypeError, ValueError):
        return None
    sentence_body = _review_based_sentence(detail_override or _public_detail_for_sentence(item), polarity=polarity)
    if len(sentence_body) < 18 or re.search(r"([가-힣A-Za-z])\1{5,}", sentence_body):
        return None
    sentence = f"{sentence_body} (review_id={review_id})."
    if 35 <= len(sentence) <= 180:
        return sentence
    return None


def _fallback_natural_items_from_evidence(
    evidence_items: list[dict[str, Any]],
    *,
    polarities: tuple[str, ...],
    existing: list[str],
    limit: int,
    sentence_polarity: str | None = None,
) -> list[str]:
    result = list(existing)
    seen_ids = {int(match.group(1)) for text in result for match in re.finditer(r"review_id\s*=\s*(\d+)", text)}
    for item in evidence_items:
        item_polarity = str(item.get("polarity"))
        if item_polarity not in polarities:
            continue
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        if review_id in seen_ids:
            continue
        detail = _sanitize_public_text(_public_detail_for_sentence(item))
        has_negative_detail = _has_negative_detail(detail)
        target_polarity = sentence_polarity or ("positive" if item_polarity == "positive" else "negative")
        detail_override = None
        if target_polarity == "positive" and has_negative_detail:
            detail_override = _positive_clause(detail)
            if not detail_override:
                continue
        if target_polarity != "positive" and item_polarity != "negative" and any(term in detail for term in POSITIVE_DETAIL_TERMS) and not has_negative_detail:
            continue
        if target_polarity != "positive" and not has_negative_detail:
            continue
        if item_polarity == "mixed" and target_polarity == "positive":
            detail_override = detail_override or _positive_clause(detail)
            if not detail_override:
                continue
        sentence = _evidence_sentence_v2(item, polarity=target_polarity, detail_override=detail_override)
        if sentence is None:
            continue
        result.append(sentence)
        seen_ids.add(review_id)
        if len(result) >= limit:
            break
    return result


def _fallback_aspect_scores_from_evidence(
    evidence_items: list[dict[str, Any]],
    existing: Any,
) -> dict[str, Any]:
    if isinstance(existing, dict) and existing:
        return existing

    counts: dict[str, dict[str, int]] = {}
    for item in evidence_items:
        aspect = str(item.get("aspect") or "").strip().lower()
        if aspect not in ASPECT_LABELS:
            continue
        polarity = str(item.get("polarity") or "").strip().lower()
        bucket = counts.setdefault(aspect, {"positive": 0, "mixed": 0, "negative": 0})
        if polarity in bucket:
            bucket[polarity] += 1

    result: dict[str, Any] = {}
    for aspect, bucket in counts.items():
        total = sum(bucket.values())
        if total <= 0:
            continue
        score = 5.0 + (bucket["positive"] * 1.2) - (bucket["negative"] * 1.4) + (bucket["mixed"] * 0.1)
        score = round(max(2.0, min(9.0, score)), 1)
        result[aspect] = {
            "label": ASPECT_LABELS.get(aspect, "리뷰"),
            "score": score,
        }
    return result


def _fallback_one_liner_from_evidence(evidence_items: list[dict[str, Any]]) -> str:
    def one_liner_rank(item: dict[str, Any]) -> int:
        detail = _sanitize_public_text(_public_detail_for_sentence(item))
        if _is_low_quality_detail(detail):
            return 9
        if str(item.get("polarity")) == "positive" and not _has_negative_detail(detail):
            return 0
        if str(item.get("polarity")) == "mixed" and any(term in detail for term in POSITIVE_DETAIL_TERMS):
            return 1
        if str(item.get("polarity")) == "positive":
            return 2
        return 3

    ordered_items = sorted(evidence_items, key=one_liner_rank)
    for item in ordered_items:
        try:
            review_id = int(item.get("review_id"))
        except (TypeError, ValueError):
            continue
        detail = _sanitize_public_text(_public_detail_for_sentence(item))
        if _is_low_quality_detail(detail):
            continue
        raw_polarity = str(item.get("polarity"))
        has_negative_detail = _has_negative_detail(detail)
        has_positive_detail = any(term in detail for term in POSITIVE_DETAIL_TERMS)
        polarity = "positive" if raw_polarity == "positive" or (raw_polarity == "mixed" and has_positive_detail) else "negative"
        if polarity == "positive" and has_negative_detail:
            detail = _positive_clause(detail)
            if not detail:
                continue
        sentence_body = _review_based_sentence(detail, polarity=polarity)
        if len(sentence_body) < 18 or re.search(r"([가-힣A-Za-z])\1{5,}", sentence_body):
            continue
        sentence = f"{sentence_body} (review_id={review_id})."
        if 35 <= len(sentence) <= 220:
            return sentence
    return ""


def _fallback_user_summary_from_evidence(evidence_items: list[dict[str, Any]], *, limit: int = 5) -> str:
    sentences: list[str] = []
    positive_items = _fallback_natural_items_from_evidence(
        evidence_items,
        polarities=("positive",),
        existing=[],
        limit=3,
    )
    if len(positive_items) < 3:
        positive_items = _fallback_natural_items_from_evidence(
            evidence_items,
            polarities=("mixed",),
            existing=positive_items,
            limit=3,
            sentence_polarity="positive",
        )
    negative_items = _fallback_natural_items_from_evidence(
        evidence_items,
        polarities=("negative",),
        existing=[],
        limit=2,
    )
    if len(negative_items) < 2:
        negative_items = _fallback_natural_items_from_evidence(
            evidence_items,
            polarities=("mixed",),
            existing=negative_items,
            limit=2,
        )
    for item in positive_items + negative_items:
        if item not in sentences:
            sentences.append(item)
        if len(sentences) >= limit:
            break
    return " ".join(sentences)


def _sanitize_keyword_list(values: Any) -> list[str]:
    return [_sanitize_public_text(item) for item in _to_string_list(values)]


def _sanitize_bucket(bucket: BucketSummary | None, evidence_index: dict[int, str] | None = None) -> BucketSummary | None:
    if bucket is None:
        return None
    return BucketSummary(
        summary=_sanitize_grounded_text(bucket.summary, evidence_index),
        sentiment_overall=bucket.sentiment_overall,
        sentiment_score=bucket.sentiment_score,
        pros=_sanitize_public_list(bucket.pros, evidence_index),
        cons=_sanitize_public_list(bucket.cons, evidence_index),
        keywords=_sanitize_keyword_list(bucket.keywords),
    )


def _has_min_evidence(payloads: list[dict[str, Any]], minimum: int = 5) -> bool:
    return len(_evidence_subset(payloads, limit=minimum)) >= minimum


async def run_feature_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    timeout_sec: int = 180,
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    prior_summary_text: str | None = None,
    representative_quotes: list[str] | None = None,
    map_summaries: list[str] | None = None,
) -> FinalSummary:
    if map_summaries is not None and not grouped_summaries:
        grouped_summaries = {"all": map_summaries}

    client = AsyncGroq(api_key=api_key)
    user_payloads = _parse_map_payloads(grouped_summaries.get("user") or grouped_summaries.get("all", []))
    critic_payloads = _parse_map_payloads(grouped_summaries.get("critic", []))
    early_payloads = _parse_map_payloads(grouped_summaries.get("early", []))
    mid_payloads = _parse_map_payloads(grouped_summaries.get("mid", []))
    late_payloads = _parse_map_payloads(grouped_summaries.get("late", []))

    usage: dict[str, Any] = {}
    try:
        user_contract = {
            "summary": "5-7 detailed Korean sentences; include at least 4 concrete evidence details and review_id anchors when possible",
            "sentiment_overall": "positive|mixed|negative",
            "sentiment_score": "0..100",
            "pros": "4-5 concrete evidence-backed Korean strings with situation/detail, not labels",
            "cons": "3-4 concrete evidence-backed Korean strings with failure mode or frustration detail",
            "keywords": "6-8 evidence-backed topics; include specific terms like boss, area, crash, pathfinding when present",
            "recommended_for": "3-5 evidence-backed player types with concrete reason",
            "caution_for": "3-5 evidence-backed caveats with concrete reason",
        }
        user_data, usage["user"] = await _run_feature_json(
            client=client,
            model_name=model_name,
            feature="user",
            timeout_sec=timeout_sec,
            prompt=_build_feature_prompt(
                feature="user",
                language_code=language_code,
                payloads=user_payloads,
                output_contract=user_contract,
                score_anchors=score_anchors,
                category_frequency=category_frequency,
                representative_quotes=representative_quotes,
                evidence_limit=14,
            ),
        )

        critic_data: dict[str, Any] | None = None
        if critic_payloads:
            critic_contract = {
                "summary": "6-8 Korean sentences about critic evaluation criteria and concrete praise/criticism",
                "sentiment_overall": "positive|mixed|negative",
                "sentiment_score": "0..100",
                "pros": "4-6 strings",
                "cons": "3-5 strings",
                "keywords": "6-10 strings",
                "evaluation_criteria": "4-6 strings",
            }
            critic_data, usage["critic"] = await _run_feature_json(
                client=client,
                model_name=model_name,
                feature="critic",
                timeout_sec=timeout_sec,
                prompt=_build_feature_prompt(
                    feature="critic",
                    language_code=language_code,
                    payloads=critic_payloads,
                    output_contract=critic_contract,
                    score_anchors=score_anchors,
                    evidence_limit=20,
                ),
            )

        playtime_contract = {
            "early": "object with 3-4 sentence summary/pros/cons/keywords or null",
            "mid": "object with 3-4 sentence summary/pros/cons/keywords or null",
            "late": "object with 3-4 sentence summary/pros/cons/keywords or null",
        }
        valid_playtime_buckets = {
            "early": _has_min_evidence(early_payloads),
            "mid": _has_min_evidence(mid_payloads),
            "late": _has_min_evidence(late_payloads),
        }
        playtime_payloads = []
        if valid_playtime_buckets["early"]:
            playtime_payloads.extend(early_payloads)
        if valid_playtime_buckets["mid"]:
            playtime_payloads.extend(mid_payloads)
        if valid_playtime_buckets["late"]:
            playtime_payloads.extend(late_payloads)
        playtime_data: dict[str, Any] = {}
        if sum(1 for is_valid in valid_playtime_buckets.values() if is_valid) >= 2:
            playtime_data, usage["playtime"] = await _run_feature_json(
                client=client,
                model_name=model_name,
                feature="playtime",
                timeout_sec=timeout_sec,
                prompt=_build_feature_prompt(
                    feature="playtime",
                    language_code=language_code,
                    payloads=[],
                    output_contract=playtime_contract,
                    extra={
                        "valid_buckets": valid_playtime_buckets,
                        "early_evidence": _evidence_subset(early_payloads, limit=8) if valid_playtime_buckets["early"] else [],
                        "mid_evidence": _evidence_subset(mid_payloads, limit=8) if valid_playtime_buckets["mid"] else [],
                        "late_evidence": _evidence_subset(late_payloads, limit=8) if valid_playtime_buckets["late"] else [],
                    },
                    evidence_limit=0,
                ),
            )

        final_contract = {
            "one_liner": "one Korean sentence under 100 chars with a concrete evidence-backed tradeoff",
            "aspect_scores": "4-7 evidence-backed aspect labels and scores",
            "sentiment_overall": "positive|mixed|negative",
            "sentiment_score": "0..100",
            "pros": "4-5 concrete evidence-backed Korean strings with situation/detail, not labels",
            "cons": "3-4 concrete evidence-backed Korean strings with failure mode or frustration detail",
            "keywords": "6-8 evidence-backed topics; prefer specific evidence topics over broad genre labels",
        }
        final_data, usage["final"] = await _run_feature_json(
            client=client,
            model_name=model_name,
            feature="final",
            timeout_sec=timeout_sec,
            prompt=_build_feature_prompt(
                feature="final",
                language_code=language_code,
                payloads=user_payloads + critic_payloads,
                output_contract=final_contract,
                score_anchors=score_anchors,
                category_frequency=category_frequency,
                representative_quotes=representative_quotes,
                evidence_limit=6,
                extra={
                    "user_summary": user_data,
                    "critic_summary": critic_data,
                    "playtime_summary": playtime_data,
                    "prior_summary_text": prior_summary_text[:1200] if prior_summary_text else None,
                },
            ),
        )

        input_tokens = sum(int(item.get("input_tokens", 0) or 0) for item in usage.values())
        output_tokens = sum(int(item.get("output_tokens", 0) or 0) for item in usage.values())
        final_evidence_items = _evidence_subset(user_payloads + critic_payloads, limit=80)
        final_evidence_index = _evidence_text_index(user_payloads + critic_payloads)
        final_pros: list[str] = []
        final_cons: list[str] = []
        final_pros = _fallback_natural_items_from_evidence(final_evidence_items, polarities=("positive",), existing=final_pros, limit=5)
        if len(final_pros) < 3:
            final_pros = _fallback_natural_items_from_evidence(
                final_evidence_items,
                polarities=("mixed",),
                existing=final_pros,
                limit=5,
                sentence_polarity="positive",
            )
        final_cons = _fallback_natural_items_from_evidence(final_evidence_items, polarities=("negative",), existing=final_cons, limit=4)
        if len(final_cons) < 2:
            final_cons = _fallback_natural_items_from_evidence(final_evidence_items, polarities=("mixed",), existing=final_cons, limit=4)
        if len(final_cons) < 2:
            final_cons = _fallback_natural_items_from_evidence(
                final_evidence_items,
                polarities=("positive",),
                existing=final_cons,
                limit=4,
                sentence_polarity="negative",
            )
        one_liner = _fallback_one_liner_from_evidence(final_evidence_items)
        if not one_liner:
            one_liner = _sanitize_grounded_text(final_data.get("one_liner", ""), final_evidence_index)
        user_bucket = _sanitize_bucket(_parse_feature_bucket(user_data), final_evidence_index)
        fallback_user_summary = _fallback_user_summary_from_evidence(final_evidence_items)
        if user_bucket is not None and fallback_user_summary:
            user_bucket.summary = fallback_user_summary
        elif user_bucket is None and fallback_user_summary:
            user_bucket = BucketSummary(
                summary=fallback_user_summary,
                sentiment_overall=_normalize_sentiment_overall(final_data.get("sentiment_overall")),
                sentiment_score=_normalize_sentiment_score(final_data.get("sentiment_score")),
                pros=final_pros,
                cons=final_cons,
                keywords=_sanitize_keyword_list(final_data.get("keywords", [])),
            )
        aspect_scores = _fallback_aspect_scores_from_evidence(final_evidence_items, final_data.get("aspect_scores", {}))

        return FinalSummary(
            one_liner=one_liner,
            aspect_scores=aspect_scores,
            full_text="",
            sentiment_overall=_normalize_sentiment_overall(final_data.get("sentiment_overall")),
            sentiment_score=_normalize_sentiment_score(final_data.get("sentiment_score")),
            pros=final_pros,
            cons=final_cons,
            keywords=_sanitize_keyword_list(final_data.get("keywords", [])),
            playtime_early=_sanitize_bucket(_parse_feature_bucket(playtime_data.get("early") if isinstance(playtime_data, dict) else None), final_evidence_index),
            playtime_mid=_sanitize_bucket(_parse_feature_bucket(playtime_data.get("mid") if isinstance(playtime_data, dict) else None), final_evidence_index),
            playtime_late=_sanitize_bucket(_parse_feature_bucket(playtime_data.get("late") if isinstance(playtime_data, dict) else None), final_evidence_index),
            critic=_sanitize_bucket(_parse_feature_bucket(critic_data), final_evidence_index),
            user=user_bucket,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            reduce_usage=usage,
        )
    except Exception as exc:
        error_code, is_retryable = classify_reduce_error(exc)
        logger.warning("feature reduce failed: code=%s retryable=%s error=%s", error_code, is_retryable, exc)
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            error_code=error_code,
            is_retryable=is_retryable,
            reduce_usage=usage,
        )


def _build_user_prompt(
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    score_anchors: dict[str, float | None] | None,
    category_frequency: list[tuple[str, int, float]] | None,
    prior_summary_text: str | None,
    max_items_map: dict[str, int] | None = None,
    chunk_length_map: dict[str, int] | None = None,
    representative_quotes: list[str] | None = None,
) -> str:
    sections: list[str] = [f"language={language_code}"]

    if representative_quotes:
        block = "[representative_quotes] (use to ground one_liner, pros, cons, user summary, and critic summary in actual review language)\n"
        for i, q in enumerate(representative_quotes, 1):
            block += f"[Q{i}] {q}\n"
        sections.append(block.rstrip())

    if score_anchors:
        block = "[score_anchors]\n"
        if score_anchors.get("steam_recommend_ratio") is not None:
            block += f"steam_recommend_ratio: {score_anchors['steam_recommend_ratio']:.2f}%\n"
        if score_anchors.get("metacritic_critic_avg") is not None:
            block += f"metacritic_critic_avg: {score_anchors['metacritic_critic_avg']:.2f}\n"
        if score_anchors.get("metacritic_user_avg") is not None:
            block += f"metacritic_user_avg: {score_anchors['metacritic_user_avg']:.2f}\n"
        if score_anchors.get("steam_recommend_ratio") is not None:
            ratio = score_anchors["steam_recommend_ratio"]
            block += (
                f"→ unified.sentiment_score MUST equal round({ratio:.0f}) ± 8. "
                "This is anchored to actual recommendation ratio. "
                "Do NOT lower it based on volume of negative content in chunks "
                "(negative reviews are inherently more verbose)."
            )
        else:
            block += "→ unified.sentiment_score must be calibrated to these numbers."
        sections.append(block)

    if category_frequency:
        block = "[category_stats]\n"
        pros_hints, cons_hints = [], []
        for cat, count, pos_ratio in category_frequency:
            pct = int(pos_ratio * 100)
            block += f"{cat}: {count}건, {pct}% 긍정\n"
            if pos_ratio >= 0.65:
                pros_hints.append(cat)
            elif pos_ratio < 0.35:
                cons_hints.append(cat)
        if pros_hints:
            block += f"→ pros 후보: {', '.join(pros_hints)}\n"
        if cons_hints:
            block += f"→ cons 후보: {', '.join(cons_hints)}\n"
        block += "→ keywords must include top-frequency categories.\n"
        mapped = [
            f"{cat} → aspect_scores.{ASPECT_KEY_MAP[cat.lower()]}"
            for cat, _, _ in category_frequency
            if cat.lower() in ASPECT_KEY_MAP
        ]
        if mapped:
            block += f"→ category-to-aspect mapping: {'; '.join(mapped)}"
        sections.append(block)

    if prior_summary_text:
        sections.append(
            f"[previous_summary] (prior synthesis — use as baseline context; "
            f"update any points where new map groups provide conflicting evidence; "
            f"new map evidence takes precedence over this on any conflicting points)\n"
            f"{prior_summary_text[:1200]}"
        )

    # 그룹별 map 요약 추가
    # 그룹별로 최대 아이템 수와 청크 길이를 조정하여 토큰 사용을 최적화
    default_chunk_len = 900
    chunk_len_map = chunk_length_map or {
        "all": 700,
        "early": 500,
        "mid": 500,
        "late": 500,
        "critic": 600,
        "user": 700,
    }
    max_map = max_items_map or {}

    for group_key in ("all", "early", "mid", "late", "critic", "user"):
        items = grouped_summaries.get(group_key, [])
        max_for_group = int(max_map.get(group_key, max(1, min(len(items), 24))))
        pick_len = chunk_len_map.get(group_key, default_chunk_len)
        picked = [item[:pick_len] for item in items[:max_for_group]]
        header = f"[group:{group_key}] ({len(picked)} chunks)"
        body = "\n\n".join(f"[map_{i+1}] {s}" for i, s in enumerate(picked)) if picked else "(empty)"
        sections.append(f"{header}\n{body}")

    return "\n\n".join(sections)


async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    grouped_summaries: dict[str, list[str]],
    max_items: int = 24,
    timeout_sec: int = 180,
    score_anchors: dict[str, float | None] | None = None,
    category_frequency: list[tuple[str, int, float]] | None = None,
    prior_summary_text: str | None = None,
    representative_quotes: list[str] | None = None,
    # 하위 호환: map_summaries 단독 전달 시 all 그룹으로 처리
    map_summaries: list[str] | None = None,
) -> FinalSummary:
    # 하위 호환: map_summaries만 전달된 경우
    if map_summaries is not None and not grouped_summaries:
        grouped_summaries = {"all": map_summaries}

    all_summaries = grouped_summaries.get("all", [])
    logger.info(
        "reduce stage started: language=%s all=%d early=%d mid=%d late=%d critic=%d user=%d",
        language_code,
        len(all_summaries),
        len(grouped_summaries.get("early", [])),
        len(grouped_summaries.get("mid", [])),
        len(grouped_summaries.get("late", [])),
        len(grouped_summaries.get("critic", [])),
        len(grouped_summaries.get("user", [])),
    )

    if not all_summaries:
        logger.warning("reduce stage skipped: no map summaries provided")
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            error_code="parse_error",
            is_retryable=False,
        )

    client = AsyncGroq(api_key=api_key)

    # 그룹별 max_items 및 청크 길이 맵을 계산하여 토큰 사용을 최적화
    max_items_map = {
        "all": min(20, len(all_summaries)),
        "early": min(8, len(grouped_summaries.get("early", []))),
        "mid": min(8, len(grouped_summaries.get("mid", []))),
        "late": min(6, len(grouped_summaries.get("late", []))),
        "critic": min(6, len(grouped_summaries.get("critic", []))),
        "user": min(20, len(grouped_summaries.get("user", []))),
    }
    chunk_length_map = {
        "all": 900,
        "early": 500,
        "mid": 500,
        "late": 500,
        "critic": 600,
        "user": 900,
    }

    # 중복 제거: 각 그룹 내에서만 중복 제거 (그룹 간 공유 금지)
    # 같은 청크 텍스트가 all/early/mid/late에 모두 포함되는 구조이므로
    # global_seen을 공유하면 버킷 그룹이 전부 비워짐
    deduped: dict[str, list[str]] = {}
    for key in ("all", "early", "mid", "late", "critic", "user"):
        items = grouped_summaries.get(key, []) or []
        seen: set[str] = set()
        deduped_items: list[str] = []
        for item in items:
            norm = " ".join(str(item).split()).strip().lower()
            if not norm or norm in seen:
                continue
            deduped_items.append(item)
            seen.add(norm)
        deduped[key] = deduped_items

    logger.info(
        "reduce input dedup: all=%d early=%d mid=%d late=%d critic=%d user=%d -> deduped: all=%d early=%d mid=%d late=%d critic=%d user=%d",
        len(grouped_summaries.get("all", [])),
        len(grouped_summaries.get("early", [])),
        len(grouped_summaries.get("mid", [])),
        len(grouped_summaries.get("late", [])),
        len(grouped_summaries.get("critic", [])),
        len(grouped_summaries.get("user", [])),
        len(deduped.get("all", [])),
        len(deduped.get("early", [])),
        len(deduped.get("mid", [])),
        len(deduped.get("late", [])),
        len(deduped.get("critic", [])),
        len(deduped.get("user", [])),
    )

    user_prompt = _build_user_prompt(
        language_code=language_code,
        grouped_summaries=deduped,
        score_anchors=score_anchors,
        category_frequency=category_frequency,
        prior_summary_text=prior_summary_text,
        max_items_map=max_items_map,
        chunk_length_map=chunk_length_map,
        representative_quotes=representative_quotes,
    )


    try:
        response = await asyncio.wait_for(
            _generate_reduce_response(client, model_name, REDUCE_SYSTEM_PROMPT, user_prompt),
            timeout=timeout_sec,
        )

        raw_text = (response.choices[0].message.content or "").strip()
        logger.info("reduce stage response received: %d chars", len(raw_text))
        try:
            parsed = _safe_parse_json(raw_text)
        except Exception as exc:
            raise ReduceParseError(str(exc)) from exc

        token_in  = int(response.usage.prompt_tokens or 0)
        token_out = int(response.usage.completion_tokens or 0)

        unified = parsed.get("unified", {})

        if not _is_valid_unified(unified):
            logger.warning("reduce output failed validation, retrying once")
            retry_response = await asyncio.wait_for(
                _generate_reduce_response(client, model_name, REDUCE_SYSTEM_PROMPT, user_prompt),
                timeout=timeout_sec,
            )
            retry_raw = (retry_response.choices[0].message.content or "").strip()
            try:
                retry_parsed = _safe_parse_json(retry_raw)
                retry_unified = retry_parsed.get("unified", {})
                if _is_valid_unified(retry_unified):
                    parsed = retry_parsed
                    unified = retry_unified
                    token_in += int(retry_response.usage.prompt_tokens or 0)
                    token_out += int(retry_response.usage.completion_tokens or 0)
                    logger.info("reduce retry succeeded validation")
                else:
                    logger.warning("reduce retry also failed validation, using first result")
            except Exception as exc:
                logger.warning("reduce retry parse failed: %s; using first result", exc)

        playtime = parsed.get("playtime", {}) or {}
        critic_data = parsed.get("critic")
        user_data = parsed.get("user")

        logger.info("reduce stage completed successfully")

        return FinalSummary(
            one_liner=str(unified.get("one_liner", "")),
            aspect_scores=unified.get("aspect_scores", {}),
            full_text=str(unified.get("full_text", "")),
            sentiment_overall=_normalize_sentiment_overall(unified.get("sentiment_overall")),
            sentiment_score=_normalize_sentiment_score(unified.get("sentiment_score")),
            pros=_to_string_list(unified.get("pros", [])),
            cons=_to_string_list(unified.get("cons", [])),
            keywords=_to_string_list(unified.get("keywords", [])),
            playtime_early=_parse_bucket(playtime.get("early")),
            playtime_mid=_parse_bucket(playtime.get("mid")),
            playtime_late=_parse_bucket(playtime.get("late")),
            critic=_parse_bucket(critic_data),
            user=_parse_bucket(user_data),
            input_tokens=token_in,
            output_tokens=token_out,
        )

    except Exception as e:
        error_code, is_retryable = classify_reduce_error(e)
        logger.warning(
            "reduce stage failed: code=%s retryable=%s error=%s",
            error_code, is_retryable, e,
        )
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            full_text=f"ErrorCode={error_code}; retryable={str(is_retryable).lower()}; detail={str(e)}",
            error_code=error_code,
            is_retryable=is_retryable,
        )
