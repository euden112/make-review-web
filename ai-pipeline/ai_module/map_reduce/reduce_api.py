from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import groq as _groq_module
from groq import AsyncGroq
from tenacity import retry, stop_after_attempt, wait_exponential
from ai_module.map_reduce.key_rotator import GroqKeyRotator

from ai_module.map_reduce.map_schema import SPOILER_TERM_PATTERNS, _redact_spoiler_terms


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# map_schema._redact_spoiler_terms가 스포일러 고유명사를 치환할 때 쓰는 placeholder.
# 두 모듈이 동일 문자열을 공유해야 공개 문장 품질 가드가 일관되게 동작한다.
REDACTION_PLACEHOLDER = "후반부 핵심 요소"

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
    "오픈월드",
    "오픈 월드",
    "자유도",
    "싱글",
    "운전",
    "대사",
    "빌드",
    "영체",
    "제작템",
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
    "price_value": "가격",
    "sound": "음향",
    "music": "음향",
    "audio": "음향",
    "gameplay": "재미",
    "fun": "재미",
    "difficulty": "난이도",
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
    review_count: int | None = None


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
    # "이런 사람에게 추천": user reduce가 생성하는 game별 플레이어 유형 + 근거.
    # 각 항목 {label, reason}. (이전엔 생성 후 폐기되어 엔드포인트가 카테고리별
    # 하드코딩 문구를 써 모든 게임이 동일했음 → 실데이터로 교체.)
    recommended_for: list[dict[str, str]] = field(default_factory=list)
    caution_for: list[dict[str, str]] = field(default_factory=list)
    # 메타
    input_tokens: int = 0
    output_tokens: int = 0
    reduce_usage: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    is_retryable: bool | None = None


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


def classify_reduce_error(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, ReduceParseError):
        return ("parse_error", False)

    message = str(exc).lower()
    if isinstance(exc, TimeoutError) or "timeout" in message or "timed out" in message:
        return ("timeout", True)

    if "quota" in message or "rate limit" in message or "429" in message:
        return ("quota", False)

    return ("upstream_unavailable", True)


@retry(stop=stop_after_attempt(5), wait=wait_exponential(multiplier=2, min=2, max=60), reraise=True)
async def _generate_reduce_response(
    client: AsyncGroq,
    model_name: str,
    system_prompt: str,
    user_prompt: str,
):
    # qwen3 계열은 기본 chain-of-thought를 출력해 토큰을 폭증시킨다(특히 TPM 한도가 낮은
    # 모델에서 치명적). /no_think 디렉티브로 reasoning을 끄고 JSON만 출력하게 한다.
    if "qwen3" in model_name.lower():
        system_prompt = system_prompt + " /no_think"
        user_prompt = user_prompt + " /no_think"
    return await client.chat.completions.create(
        model=model_name,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        response_format={"type": "json_object"},
        # 온도 0: aspect delta·sentiment delta·버킷 텍스트가 run마다 흔들리는 일관성
        # 문제를 줄인다. (env REDUCE_TEMPERATURE로 조정 가능.)
        temperature=float(os.getenv("REDUCE_TEMPERATURE", "0")),
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
- NEVER quote review text verbatim in a foreign language. If the source review is English (or any non-Korean), paraphrase its meaning in natural Korean. The output must be Korean only; do not paste English sentences.
- Do NOT enumerate quotes like "리뷰어는 '...'라고 작성했습니다" or "한 리뷰는 ...라고 했습니다" repeatedly. Synthesize multiple reviews into one flowing Korean explanation instead of listing individual quotes.
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
    # polarity별로 그룹화 후 라운드로빈 인터리브한다. 이전엔 부정 먼저 정렬 후 limit
    # 컷이라, limit이 작은 playtime 버킷(8)에서 상위가 전부 부정으로 채워져 95% 긍정
    # 게임인데도 요약/cons가 불만 일색이 되는 편향이 있었다. 긍정을 먼저 두어 긍정 우세
    # 게임의 톤을 반영하되, 부정·mixed도 매 라운드 섞여 cons 재료도 보존한다.
    buckets_by_pol: dict[str, list[dict[str, Any]]] = {"positive": [], "negative": [], "mixed": []}
    for item in rows:
        pol = str(item.get("polarity"))
        buckets_by_pol.setdefault(pol if pol in buckets_by_pol else "mixed", []).append(item)
    for pol in buckets_by_pol:
        buckets_by_pol[pol].sort(key=lambda item: (str(item.get("aspect", "")), int(item.get("review_id") or 0)))
    interleaved: list[dict[str, Any]] = []
    order = ["positive", "negative", "mixed"]
    while any(buckets_by_pol[p] for p in order):
        for p in order:
            if buckets_by_pol[p]:
                interleaved.append(buckets_by_pol[p].pop(0))
    return interleaved[:limit]


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
    rotator: GroqKeyRotator,
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
    last_exc: Exception | None = None
    for attempt in range(rotator.key_count):
        client = rotator.make_client()
        try:
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
                "retry": attempt,
            }
            logger.info(
                "feature reduce completed: feature=%s input_tokens=%d output_tokens=%d chars=%d key_index=%d",
                feature, usage["input_tokens"], usage["output_tokens"], len(raw_text), attempt,
            )
            return parsed, usage
        except _groq_module.RateLimitError as e:
            last_exc = e
            logger.warning("Groq 429 on feature=%s key %d/%d, rotating...", feature, attempt + 1, rotator.key_count)
            rotator.rotate()
    raise last_exc


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
        "좆같아서": "불합리하게 느껴져서",
        "좆같": "불합리하게 느껴지",
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
    # "일부 리뷰어들은"→"한 리뷰는들은" 같은 부분 치환 잔재 정리
    sanitized = sanitized.replace("리뷰는들은", "리뷰는").replace("리뷰는들이", "리뷰는").replace("리뷰는들", "리뷰는")
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


_BROKEN_JOSA_LEAD = re.compile(r"^[은는이가을를과와로의도만에]\s+\S")


def _repair_period_josa(text: str) -> str:
    # LLM이 "…좋다.는" 처럼 마침표 직후 조사를 붙여 두 문장을 잇는 경우, 사이에 공백을 삽입해
    # 정상 문장 분할기가 동작하도록 한다.
    return re.sub(r"([.!?。])([은는이가을를과와로의도만에])(?=[가-힣\s])", r"\1 \2", str(text or ""))


def _sanitize_grounded_text(text: Any, evidence_index: dict[int, str] | None = None) -> str:
    sanitized = _repair_review_id_anchors(_sanitize_public_text(str(text or "")), evidence_index)
    sanitized = _repair_period_josa(sanitized)
    if not evidence_index:
        return sanitized
    segments = [part.strip() for part in re.split(r"(?<=[.!?。])\s+", sanitized) if part.strip()]
    if not segments:
        return sanitized
    kept: list[str] = []
    seen_segments: set[str] = set()
    for segment in segments:
        if _is_vague_public_sentence(segment):
            continue
        if _segment_anchor_failures(segment, evidence_index):
            continue
        # 마침표 직후 떨어져 나온 잔존 조사 시작 문장은 의미 단위가 깨진 파편이므로 제거.
        if _BROKEN_JOSA_LEAD.match(segment):
            continue
        # 동일/유사 문장 반복 제거 — LLM이 같은 지적을 여러 번 되풀이하는 경우 방지
        seg_key = _sentence_body_key(segment)
        if seg_key in seen_segments:
            continue
        seen_segments.add(seg_key)
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


def _sentence_body_key(text: str) -> str:
    """review_id 앵커를 제거한 문장 본문 정규화 키.

    룰 엔진/템플릿은 서로 다른 evidence(다른 review_id)를 같은 문장으로
    환원할 수 있다. review_id로만 dedup하면 동일 텍스트가 중복 노출되므로,
    앵커를 떼어낸 본문 기준으로도 중복을 제거한다.
    """
    body = re.sub(r"\s*\(review_id\s*=\s*\d+\)\s*\.?\s*$", "", str(text or "")).strip()
    return " ".join(body.split())


def _fallback_items_from_evidence(
    evidence_items: list[dict[str, Any]],
    *,
    polarity: str,
    existing: list[str],
    limit: int,
) -> list[str]:
    result = list(existing)
    seen_ids = {int(match.group(1)) for text in result for match in re.finditer(r"review_id\s*=\s*(\d+)", text)}
    seen_bodies = {_sentence_body_key(text) for text in result}
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
        if detail.count(REDACTION_PLACEHOLDER) >= 2:
            continue
        sentence = f"{detail}라는 실제 리뷰 근거가 있습니다 (review_id={review_id})."
        body = _sentence_body_key(sentence)
        if body in seen_bodies:
            continue
        if 35 <= len(sentence) <= 260:
            result.append(sentence)
            seen_ids.add(review_id)
            seen_bodies.add(body)
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
    # 스포일러 redaction placeholder가 한 문장에 여러 번 들어가면 스포일러 과밀
    # 원문이라 redaction 후에도 "후반부 핵심 요소 ... 후반부 핵심 요소 ..."처럼
    # 깨진 문장이 된다. 공개 문장 후보에서 제외한다.
    if normalized.count(REDACTION_PLACEHOLDER) >= 2:
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
    has_grounding = _has_grounding_detail(normalized)
    has_sentiment = any(term in normalized for term in POSITIVE_DETAIL_TERMS + NEGATIVE_DETAIL_TERMS)
    if has_grounding and has_sentiment:
        return "accept"
    if len(normalized) < 18:
        return "ambiguous"
    return "ambiguous"


def _has_grounding_detail(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return any(term.lower() in normalized for term in GROUNDING_TERMS)


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
    if decision != "accept":
        return ""

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
        if aspect == "controls":
            body = f"조작 흐름에서 {detail}는 식의 호평이 확인됩니다"
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
    seen_bodies = {_sentence_body_key(text) for text in result}
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
        if detail.count(REDACTION_PLACEHOLDER) >= 2:
            continue
        if len(detail) > 110:
            detail = detail[:110].rstrip() + "..."
        sentence = f"{detail}라는 반응이 있습니다 (review_id={review_id})."
        body = _sentence_body_key(sentence)
        if body in seen_bodies:
            continue
        if 35 <= len(sentence) <= 180:
            result.append(sentence)
            seen_ids.add(review_id)
            seen_bodies.add(body)
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
    seen_bodies = {_sentence_body_key(text) for text in result}
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
        body = _sentence_body_key(sentence)
        if body in seen_bodies:
            continue
        result.append(sentence)
        seen_ids.add(review_id)
        seen_bodies.add(body)
        if len(result) >= limit:
            break
    return result


def _compute_baseline_aspect_scores(
    evidence_items: list[dict[str, Any]],
    sentiment_anchor: float | None = None,
    cumulative_counts: dict[str, dict[str, int]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Evidence 카운트 + sentiment anchor 기반 결정론 기준점 산출.

    baseline_neutral은 전체 추천률(sentiment_anchor)에서 도출해 게임 전반
    수용도를 반영한다. 그 위에 (pos − neg) × 0.5 대칭 가중으로 각 aspect의
    상대 위치를 더한다. 부정 리뷰 verbose 편향을 보정하기 위해 가중치를
    대칭화하고, 시작점을 고정 5.0에서 anchor 연동으로 변경했다.

    LLM은 이 baseline을 입력으로 받아 ±2.0 delta를 인용 근거와 함께 제안하고
    _apply_aspect_score_deltas가 검증 후 결합한다.
    """
    counts: dict[str, dict[str, int]] = {}
    cited_review_ids: dict[str, set[int]] = {}
    for item in evidence_items:
        aspect = str(item.get("aspect") or "").strip().lower()
        if aspect not in ASPECT_LABELS:
            continue
        polarity = str(item.get("polarity") or "").strip().lower()
        bucket = counts.setdefault(aspect, {"positive": 0, "mixed": 0, "negative": 0})
        if polarity in bucket:
            bucket[polarity] += 1
        try:
            rid = int(item.get("review_id"))
            cited_review_ids.setdefault(aspect, set()).add(rid)
        except (TypeError, ValueError):
            pass

    # aspect별 polarity는 map LLM evidence(문장 단위 판정)를 우선한다.
    # 누적 카운트(crawler 카테고리 태그)는 sentiment를 리뷰 전체/키워드 기준으로 붙여
    # default-positive 편향이 크다(예: 최적화 불만이 많은데도 긍정 23:부정 1). 이를 그대로
    # 덮어쓰면 불만 aspect가 긍정으로 오염돼 점수 역전이 발생한다. 따라서 map evidence가
    # 있는 aspect는 map polarity를 유지하고, map이 다루지 않은 aspect만 누적으로 폴백한다.
    if cumulative_counts:
        for asp, cc in cumulative_counts.items():
            if asp not in ASPECT_LABELS:
                continue
            map_bucket = counts.get(asp)
            if map_bucket and sum(map_bucket.values()) > 0:
                continue  # map polarity 우선
            counts[asp] = {
                "positive": int(cc.get("positive", 0)),
                "mixed": int(cc.get("mixed", 0)),
                "negative": int(cc.get("negative", 0)),
            }

    # Sentiment anchor (0~100) → baseline_neutral (0~10) 선형 매핑.
    # 50% 추천 → 5.0, 90% → 6.6, 100% → 7.0. 기울기를 낮춰
    # 9.0 ceiling 흡수로 인한 aspect 변별력 소실을 방지한다.
    if sentiment_anchor is None:
        baseline_neutral = 5.0
    else:
        try:
            anchor_val = float(sentiment_anchor)
        except (TypeError, ValueError):
            baseline_neutral = 5.0
        else:
            baseline_neutral = 5.0 + (anchor_val - 50.0) * 0.04
            baseline_neutral = max(2.5, min(7.0, baseline_neutral))

    result: dict[str, dict[str, Any]] = {}
    # 데이터(map evidence 또는 누적 태그)가 있는 aspect만 산출한다. 무데이터 aspect는
    # 점수를 지어내지 않고 누락시켜 프론트가 "데이터 부족"으로 정직하게 표시한다.
    # (graphics·controls 등은 태그 백필이 들어오면 여기서 데이터 보유 → 점수 산출.)
    for aspect, bucket in counts.items():
        total = sum(bucket.values())
        if total <= 0:
            continue
        # 비율 기반 skew: (pos − neg) / (pos + neg + 1) ∈ [-1, +1]. mixed는 분모만 키워 희석.
        skew = (bucket["positive"] - bucket["negative"]) / (bucket["positive"] + bucket["negative"] + 1)
        # 표본 수축(shrinkage): 증거가 적은 aspect는 skew를 baseline 쪽으로 끌어당겨
        # run마다 1~2건 차이로 점수가 크게 흔들리는 일관성 문제를 막는다.
        # n이 충분히 크면 full skew, 작으면 0으로 수축. K는 절반-신뢰 표본 수.
        _K = 5.0
        confidence = total / (total + _K)
        score = baseline_neutral + skew * 2.0 * confidence
        score = round(max(2.0, min(9.0, score)), 1)
        result[aspect] = {
            "label": ASPECT_LABELS.get(aspect, "리뷰"),
            "score": score,
            "evidence_count": total,
            "evidence_review_ids": sorted(cited_review_ids.get(aspect, set())),
        }
    return result


def _apply_aspect_score_deltas(
    baseline: dict[str, dict[str, Any]],
    llm_deltas: Any,
    llm_evidence: Any,
    valid_review_ids: set[int],
) -> dict[str, dict[str, Any]]:
    """Baseline 점수에 LLM delta를 검증 후 결합.

    검증 규칙:
    - delta는 [-2.0, +2.0] 범위. 초과 시 클램프.
    - non-zero delta는 aspect_delta_evidence에 ≥1개의 유효 review_id 인용 필요.
      미인용 또는 invalid id → delta 0 강제.
    - baseline에 없는 aspect의 delta는 무시 (LLM이 새 aspect 만들지 못함).
    - baseline evidence_count < 2면 delta 무시 (표본 부족 시 LLM 판단 차단).
    최종 점수는 [2.0, 9.0] 클램프.
    """
    if not isinstance(llm_deltas, dict):
        llm_deltas = {}
    if not isinstance(llm_evidence, dict):
        llm_evidence = {}

    result: dict[str, dict[str, Any]] = {}
    for aspect, base in baseline.items():
        base_score = float(base.get("score") or 5.0)
        applied_delta = 0.0
        try:
            raw_delta = float(llm_deltas.get(aspect, 0))
        except (TypeError, ValueError):
            raw_delta = 0.0
        clamped_delta = max(-2.0, min(2.0, raw_delta))
        if clamped_delta != 0.0 and int(base.get("evidence_count") or 0) >= 2:
            citations = llm_evidence.get(aspect, [])
            if isinstance(citations, list):
                valid = [
                    rid for rid in citations
                    if isinstance(rid, (int, float)) and int(rid) in valid_review_ids
                ]
                if valid:
                    applied_delta = clamped_delta
        final_score = round(max(2.0, min(9.0, base_score + applied_delta)), 1)
        result[aspect] = {
            "label": base.get("label") or ASPECT_LABELS.get(aspect, "리뷰"),
            "score": final_score,
        }
    return result


def _apply_sentiment_score_delta(
    anchor: float | None,
    llm_delta: Any,
    llm_evidence: Any,
    valid_review_ids: set[int],
    min_sample: int,
    sample_size: int,
) -> float | None:
    """Steam recommend ratio 앵커에 LLM delta를 검증 후 결합.

    검증 규칙:
    - 앵커가 None이면 반환 None (이후 _normalize_sentiment_score 경로로 폴백).
    - 표본 sample_size < min_sample이면 LLM delta 무시.
    - delta는 [-8, +8] 범위. 초과 시 클램프.
    - non-zero delta는 score_delta_evidence에 ≥2개의 유효 review_id 인용 필요.
    최종 점수는 [0, 100] 클램프 후 정수 반올림.
    """
    if anchor is None:
        return None
    try:
        anchor_f = float(anchor)
    except (TypeError, ValueError):
        return None
    if sample_size < min_sample:
        return round(max(0.0, min(100.0, anchor_f)))

    try:
        raw_delta = float(llm_delta) if llm_delta is not None else 0.0
    except (TypeError, ValueError):
        raw_delta = 0.0
    clamped_delta = max(-8.0, min(8.0, raw_delta))
    applied_delta = 0.0
    if clamped_delta != 0.0:
        citations = llm_evidence if isinstance(llm_evidence, list) else []
        valid_count = 0
        for cite in citations:
            rid = None
            if isinstance(cite, dict):
                rid = cite.get("review_id")
            elif isinstance(cite, (int, float)):
                rid = cite
            if isinstance(rid, (int, float)) and int(rid) in valid_review_ids:
                valid_count += 1
        if valid_count >= 2:
            applied_delta = clamped_delta
    return round(max(0.0, min(100.0, anchor_f + applied_delta)))


def _fallback_aspect_scores_from_evidence(
    evidence_items: list[dict[str, Any]],
    existing: Any,
) -> dict[str, Any]:
    """Deprecated: 새 경로(_compute_baseline_aspect_scores + _apply_aspect_score_deltas) 사용.

    이전 호환을 위해 유지하며 baseline만 반환.
    """
    if isinstance(existing, dict) and existing:
        return existing
    baseline = _compute_baseline_aspect_scores(evidence_items)
    return {
        aspect: {"label": data["label"], "score": data["score"]}
        for aspect, data in baseline.items()
    }


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


def _llm_summary_passes_gate(summary: str) -> bool:
    """살균을 통과한 LLM 요약을 그대로 공개할 수 있는지 판정.

    _sanitize_bucket이 이미 비속어·일반 스포일러 redaction과 vague/anchor 실패 문장
    제거를 수행하고, reduce 입력은 스포일러 redaction된 public_detail이므로 여기서는
    분량·언어·문장 수만 확인한다. 통과하면 비평가 요약처럼 LLM 산문을 유지하고,
    실패하면 결정론적 템플릿 요약으로 fallback한다.
    """
    text = " ".join(str(summary or "").split())
    if len(text) < 80:
        return False
    if re.search(r"[一-鿿]", text):  # 중국어 오염 detail이 요약에 새어든 경우
        return False
    sentences = [s for s in re.split(r"(?<=[.!?。])\s+", text) if s.strip()]
    return len(sentences) >= 2


def _has_min_evidence(payloads: list[dict[str, Any]], minimum: int = 5) -> bool:
    return len(_evidence_subset(payloads, limit=minimum)) >= minimum


def _is_degenerate_bucket(obj: Any) -> bool:
    """LLM이 버킷을 빈 pros·cons로 'phone in'한 경우 탐지.

    early+mid+late를 단일 호출·단일 JSON으로 생성하면 마지막 버킷(주로 late)이
    근거가 충분(_has_min_evidence 통과)해도 빈 배열 + 필러 요약으로 degrade된다.
    pros·cons가 둘 다 비면 degenerate로 보고 단독 재호출 대상으로 삼는다.
    """
    if not isinstance(obj, dict):
        return False
    pros = obj.get("pros") or []
    cons = obj.get("cons") or []
    return len(pros) == 0 and len(cons) == 0


def _parse_player_targets(raw: Any, *, limit: int = 5) -> list[dict[str, str]]:
    """user reduce의 recommended_for/caution_for를 {label, reason} 리스트로 정규화.

    LLM이 객체 배열을 주는 게 기본이나, 문자열 배열로 오는 경우도 방어적으로 처리한다.
    스포일러/비속어는 _sanitize_public_text로 살균하고, label 없는 항목은 버린다.
    """
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        label = ""
        reason = ""
        if isinstance(item, dict):
            label = str(item.get("label") or item.get("type") or item.get("player_type") or "").strip()
            reason = str(item.get("reason") or item.get("summary") or item.get("why") or "").strip()
        elif isinstance(item, str):
            label = item.strip()
        if not label:
            continue
        label = _sanitize_public_text(label)
        reason = _sanitize_public_text(reason) if reason else ""
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "reason": reason})
        if len(out) >= limit:
            break
    return out


def _bucket_stats(payloads: list[dict[str, Any]]) -> tuple[float | None, str | None, int]:
    """버킷 map payload들에서 결정론 sentiment_score·label·review_count 산출.

    playtime 버킷 reduce는 summary/pros/cons만 생성하고 sentiment를 주지 않으므로,
    user/critic anchor와 같은 원리로 해당 버킷 리뷰의 추천 비율에서 직접 점수를 만든다.
    score = positive/(positive+negative)*100. label은 점수에서 도출(≥60 긍정/≤45 부정/그외 중립).
    review_count는 버킷에 속한 고유 review_id 수.
    """
    pos = neg = mix = 0
    rids: set[int] = set()
    for p in payloads:
        s = p.get("sentiment") if isinstance(p, dict) else None
        if isinstance(s, dict):
            pos += int(s.get("positive") or 0)
            neg += int(s.get("negative") or 0)
            mix += int(s.get("mixed") or 0)
        for rid in (p.get("review_ids") or []):
            try:
                rids.add(int(rid))
            except (TypeError, ValueError):
                pass
        for item in (p.get("evidence_items") or []):
            try:
                rids.add(int(item.get("review_id")))
            except (TypeError, ValueError):
                pass
    total = pos + neg + mix
    # 전체 추천 비율 정의(positive / 전체)와 일치시킨다. mixed를 분모에 포함해
    # neg=0일 때 100으로 포화되는 문제를 줄이고 overall anchor와 같은 척도를 쓴다.
    if total > 0:
        score: float | None = round(pos / total * 100)
    else:
        score = None
    if score is None:
        overall = None
    elif score >= 60:
        overall = "positive"
    elif score <= 45:
        overall = "negative"
    else:
        overall = "mixed"
    return score, overall, len(rids)


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
    cumulative_aspect_counts: dict[str, dict[str, int]] | None = None,
) -> FinalSummary:
    if map_summaries is not None and not grouped_summaries:
        grouped_summaries = {"all": map_summaries}

    rotator = GroqKeyRotator.from_key_string(api_key)
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
            "recommended_for": "array of 3-5 objects {label: short Korean player-type phrase (예: '오픈월드 자유도를 즐기는 플레이어'), reason: one concrete Korean sentence grounded in this game's evidence with review_id when available}",
            "caution_for": "array of 3-5 objects {label: short Korean player-type phrase to be cautious, reason: one concrete Korean sentence grounded in this game's evidence with review_id when available}",
        }
        user_data, usage["user"] = await _run_feature_json(
            rotator=rotator,
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
                rotator=rotator,
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
                rotator=rotator,
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

        # 마지막-버킷 degrade 보정: 유효 버킷(evidence≥5)인데 결합 호출 결과 pros·cons가
        # 모두 비면 그 버킷만 단독 재호출한다(경쟁 버킷 없는 격리 프롬프트 → full effort).
        # 재호출도 degenerate면 필러 대신 null 저장(정직하게 '데이터 부족' 표기).
        if isinstance(playtime_data, dict):
            _pt_ev = {"early": early_payloads, "mid": mid_payloads, "late": late_payloads}
            for _bn in ("early", "mid", "late"):
                if not valid_playtime_buckets[_bn]:
                    continue
                if not _is_degenerate_bucket(playtime_data.get(_bn)):
                    continue
                _retry_data, _retry_usage = await _run_feature_json(
                    rotator=rotator,
                    model_name=model_name,
                    feature="playtime",
                    timeout_sec=timeout_sec,
                    prompt=_build_feature_prompt(
                        feature="playtime",
                        language_code=language_code,
                        payloads=[],
                        output_contract={_bn: "object with 3-4 sentence summary/pros/cons/keywords or null"},
                        extra={
                            "valid_buckets": {_bn: True},
                            f"{_bn}_evidence": _evidence_subset(_pt_ev[_bn], limit=8),
                        },
                        evidence_limit=0,
                    ),
                )
                _retry_bucket = _retry_data.get(_bn) if isinstance(_retry_data, dict) else None
                playtime_data[_bn] = _retry_bucket if not _is_degenerate_bucket(_retry_bucket) else None
                _pu = usage.setdefault("playtime", {})
                for _k, _v in (_retry_usage or {}).items():
                    if isinstance(_v, (int, float)):
                        _pu[_k] = _pu.get(_k, 0) + _v

        # Evidence 기반 baseline 사전 산출: aspect 점수와 sentiment anchor.
        # LLM은 이 baseline을 입력으로 받아 인용 근거가 있는 작은 delta만 제안한다.
        pre_evidence_items = _evidence_subset(user_payloads + critic_payloads, limit=80)
        pre_sentiment_anchor = None
        if score_anchors and score_anchors.get("steam_recommend_ratio") is not None:
            pre_sentiment_anchor = float(score_anchors["steam_recommend_ratio"])
        baseline_aspect_scores = _compute_baseline_aspect_scores(
            pre_evidence_items,
            sentiment_anchor=pre_sentiment_anchor,
            cumulative_counts=cumulative_aspect_counts,
        )
        baseline_aspect_for_prompt = {
            aspect: {
                "label": data["label"],
                "baseline_score": data["score"],
                "evidence_count": data["evidence_count"],
                "evidence_review_ids": data["evidence_review_ids"][:6],
            }
            for aspect, data in baseline_aspect_scores.items()
        }
        sentiment_anchor_value = None
        if score_anchors and score_anchors.get("steam_recommend_ratio") is not None:
            sentiment_anchor_value = round(float(score_anchors["steam_recommend_ratio"]))

        final_contract = {
            "one_liner": "one Korean sentence under 100 chars with a concrete evidence-backed tradeoff",
            "sentiment_overall": "positive|mixed|negative",
            "sentiment_score_delta": (
                "integer in [-8, +8] adjusting the provided sentiment_score_anchor. "
                "0 = anchor 그대로. Non-zero delta는 score_delta_evidence에 2건 이상의 review_id 인용 필수. "
                "anchor가 null이면 0을 출력."
            ),
            "score_delta_evidence": (
                "array of objects {review_id:int, why:string}. score_delta가 0이 아니면 ≥2건 필수. "
                "review_id는 입력 evidence에 존재해야 함."
            ),
            "aspect_score_deltas": (
                "object {aspect_key: float in [-2.0, +2.0]} adjusting baseline_aspect_scores. "
                "baseline에 없는 aspect는 추가 금지. 인용 없는 delta는 0으로 강제됨."
            ),
            "aspect_delta_evidence": (
                "object {aspect_key: [review_id, ...]}. Non-zero delta는 해당 aspect에 ≥1건 review_id 인용 필수."
            ),
            "pros": "4-5 concrete evidence-backed Korean strings with situation/detail, not labels",
            "cons": "3-4 concrete evidence-backed Korean strings with failure mode or frustration detail",
            "keywords": "6-8 evidence-backed topics; prefer specific evidence topics over broad genre labels",
        }
        final_data, usage["final"] = await _run_feature_json(
            rotator=rotator,
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
                    "sentiment_score_anchor": sentiment_anchor_value,
                    "baseline_aspect_scores": baseline_aspect_for_prompt,
                    "scoring_protocol": (
                        "Score는 직접 출력하지 말 것. sentiment_score_delta(앵커 대비)와 "
                        "aspect_score_deltas(baseline 대비)만 제안. 모든 non-zero delta는 인용 근거 필요. "
                        "근거 부재 시 delta=0. 코드가 anchor/baseline + 검증된 delta로 최종 점수 산출."
                    ),
                },
            ),
        )

        input_tokens = sum(int(item.get("input_tokens", 0) or 0) for item in usage.values())
        output_tokens = sum(int(item.get("output_tokens", 0) or 0) for item in usage.values())
        final_evidence_items = _evidence_subset(user_payloads + critic_payloads, limit=80)
        final_evidence_index = _evidence_text_index(user_payloads + critic_payloads)
        # pros/cons는 LLM이 한국어로 의역·종합한 결과를 우선 사용한다. 결정론 fallback은 원문
        # 스니펫(원어)을 짜깁기해 영어 리뷰가 영어로 새거나 "재밌었음는" 같은 비문이 나왔다.
        # LLM 결과는 영어 리뷰도 한국어로 반영하며, _sanitize_public_list가 review_id 근거·길이를
        # 검증한다. LLM 결과가 부족할 때만 결정론 evidence 문장으로 채운다.
        final_pros: list[str] = _sanitize_public_list(final_data.get("pros"), final_evidence_index)[:5]
        final_cons: list[str] = _sanitize_public_list(final_data.get("cons"), final_evidence_index)[:4]
        if len(final_pros) < 3:
            final_pros = _fallback_natural_items_from_evidence(final_evidence_items, polarities=("positive",), existing=final_pros, limit=5)
        if len(final_pros) < 3:
            final_pros = _fallback_natural_items_from_evidence(
                final_evidence_items,
                polarities=("mixed",),
                existing=final_pros,
                limit=5,
                sentence_polarity="positive",
            )
        if len(final_cons) < 2:
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
        # 유저 요약: LLM 산문이 게이트(분량·언어·문장수, 살균은 _sanitize_bucket에서 완료)를
        # 통과하면 비평가 요약처럼 그대로 사용한다. 통과하지 못할 때만 결정론적 템플릿으로
        # fallback해 가독성이 떨어지는 "…반응이 있습니다" 나열을 최소화한다.
        if user_bucket is not None and _llm_summary_passes_gate(user_bucket.summary):
            pass
        elif user_bucket is not None and fallback_user_summary:
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
        # Aspect 점수: baseline + LLM delta (인용 검증 후 결합)
        valid_review_ids = {
            int(item.get("review_id"))
            for item in final_evidence_items
            if isinstance(item.get("review_id"), (int, float))
        }
        aspect_scores = _apply_aspect_score_deltas(
            baseline_aspect_scores,
            final_data.get("aspect_score_deltas", {}),
            final_data.get("aspect_delta_evidence", {}),
            valid_review_ids,
        )

        # Sentiment 점수: anchor + LLM delta (인용 검증 후 결합)
        steam_total_count = 0
        if score_anchors:
            steam_total_count = int(score_anchors.get("steam_total") or 0)
        validated_sentiment_score = _apply_sentiment_score_delta(
            anchor=sentiment_anchor_value,
            llm_delta=final_data.get("sentiment_score_delta"),
            llm_evidence=final_data.get("score_delta_evidence"),
            valid_review_ids=valid_review_ids,
            min_sample=10,
            sample_size=steam_total_count or 999,
        )
        if validated_sentiment_score is None:
            validated_sentiment_score = _normalize_sentiment_score(final_data.get("sentiment_score"))

        # 라벨은 최종 점수에서 결정론적으로 도출해 LLM 라벨/점수 불일치를 차단한다.
        if isinstance(validated_sentiment_score, (int, float)):
            if validated_sentiment_score >= 60:
                derived_sentiment_overall = "positive"
            elif validated_sentiment_score <= 45:
                derived_sentiment_overall = "negative"
            else:
                derived_sentiment_overall = "mixed"
        else:
            derived_sentiment_overall = _normalize_sentiment_overall(final_data.get("sentiment_overall"))

        # 유저 요약 점수를 Steam 추천률 anchor 기반 최종 점수와 정합시킨다.
        # 추천률은 본질적으로 유저 신호이므로 user 버킷에 그대로 적용한다. 버킷 reduce LLM이
        # pros/cons를 균형 요약하며 중간대(50~60)로 수렴해 항상 "mixed"로 보이던 문제를,
        # anchor 결합 점수 + 결정론 라벨 도출로 unified와 일관되게 만든다.
        if user_bucket is not None and isinstance(validated_sentiment_score, (int, float)):
            user_bucket.sentiment_score = validated_sentiment_score
            user_bucket.sentiment_overall = derived_sentiment_overall

        # 비평가 요약 점수를 Metacritic critic 평균(이미 0~100) anchor에 정합시킨다.
        # critic 평균 자체가 평론 점수이므로 delta 없이 그대로 사용하고, 라벨은 결정론 도출.
        critic_bucket = _sanitize_bucket(_parse_feature_bucket(critic_data), final_evidence_index)
        critic_anchor = None
        if score_anchors and score_anchors.get("metacritic_critic_avg") is not None:
            try:
                critic_anchor = round(float(score_anchors["metacritic_critic_avg"]))
            except (TypeError, ValueError):
                critic_anchor = None
        if critic_bucket is not None and isinstance(critic_anchor, (int, float)):
            critic_bucket.sentiment_score = float(critic_anchor)
            if critic_anchor >= 60:
                critic_bucket.sentiment_overall = "positive"
            elif critic_anchor <= 45:
                critic_bucket.sentiment_overall = "negative"
            else:
                critic_bucket.sentiment_overall = "mixed"

        # playtime 버킷별 결정론 sentiment·review_count 부착 (reduce LLM은 summary만 생성).
        pt_payloads = {"early": early_payloads, "mid": mid_payloads, "late": late_payloads}
        playtime_buckets_out: dict[str, BucketSummary | None] = {}
        for _name in ("early", "mid", "late"):
            _raw_bucket = playtime_data.get(_name) if isinstance(playtime_data, dict) else None
            # invalid 버킷(evidence<5)은 LLM이 valid_buckets 지시를 무시하고 필러 객체를 채워
            # 반환할 수 있다. 그대로 저장하면 pros·cons 빈 필러 요약이 노출되므로,
            # 유효하지 않거나 pros·cons가 모두 빈 degenerate면 null(=데이터 부족)로 버린다.
            if not valid_playtime_buckets.get(_name) or _is_degenerate_bucket(_raw_bucket):
                playtime_buckets_out[_name] = None
                continue
            _b = _sanitize_bucket(
                _parse_feature_bucket(_raw_bucket),
                final_evidence_index,
            )
            if _b is not None:
                # 감성 점수/라벨은 실제 추천 비율(ai_service의 bucket_stats)로 채운다.
                # map payload sentiment는 추천 수가 아니라 신뢰할 수 없어 여기선 review_count만 산출.
                _, _, _count = _bucket_stats(pt_payloads[_name])
                _b.review_count = _count
            playtime_buckets_out[_name] = _b

        return FinalSummary(
            one_liner=one_liner,
            aspect_scores=aspect_scores,
            full_text="",
            sentiment_overall=derived_sentiment_overall,
            sentiment_score=validated_sentiment_score,
            pros=final_pros,
            cons=final_cons,
            keywords=_sanitize_keyword_list(final_data.get("keywords", [])),
            playtime_early=playtime_buckets_out["early"],
            playtime_mid=playtime_buckets_out["mid"],
            playtime_late=playtime_buckets_out["late"],
            critic=critic_bucket,
            user=user_bucket,
            recommended_for=_parse_player_targets(
                user_data.get("recommended_for") if isinstance(user_data, dict) else None
            ),
            caution_for=_parse_player_targets(
                user_data.get("caution_for") if isinstance(user_data, dict) else None
            ),
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
