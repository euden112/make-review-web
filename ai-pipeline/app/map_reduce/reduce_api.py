from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(slots=True)
class FinalSummary:
    one_liner: str
    aspect_scores: dict[str, Any]
    representative_reviews: list[dict[str, Any]]
    full_text: str


REDUCE_SYSTEM_PROMPT = """
You are a game review synthesis engine.
Return JSON only.
Required keys:
- one_liner: string
- aspect_scores: {
    graphics: {label, score},
    controls: {label, score},
    optimization: {label, score},
    content: {label, score},
    price_value: {label, score}
  }
- representative_reviews: [{source, review_id, quote, reason}]
- full_text: string
No markdown, no code fences.
""".strip()


GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def _safe_parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


def _extract_text_from_gemini_response(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates", [])
    if not candidates:
        raise ValueError("Gemini response has no candidates")

    first = candidates[0]
    content = first.get("content", {})
    parts = content.get("parts", [])
    if not parts:
        raise ValueError("Gemini response has no content parts")

    text_fragments: list[str] = []
    for part in parts:
        text = part.get("text")
        if text:
            text_fragments.append(text)

    if not text_fragments:
        raise ValueError("Gemini response has no text part")

    return "\n".join(text_fragments).strip()


async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    map_summaries: list[str],
    max_items: int = 24,
) -> FinalSummary:
    picked = [item[:900] for item in map_summaries[:max_items]]
    user_prompt = (
        f"language={language_code}\n"
        "Integrate map summaries into a final sentiment-aware game review summary.\n"
        "Ensure aspect_scores and representative_reviews are grounded in evidence.\n\n"
        + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
    )

    endpoint = f"{GEMINI_API_BASE}/models/{model_name}:generateContent"
    request_payload = {
        "system_instruction": {
            "parts": [{"text": REDUCE_SYSTEM_PROMPT}],
        },
        "contents": [
            {
                "role": "user",
                "parts": [{"text": user_prompt}],
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    async with httpx.AsyncClient() as client:
        response = await client.post(
            endpoint,
            params={"key": api_key},
            json=request_payload,
            timeout=120,
        )
        response.raise_for_status()
        raw_text = _extract_text_from_gemini_response(response.json())

    parsed = _safe_parse_json(raw_text)
    return FinalSummary(
        one_liner=parsed["one_liner"],
        aspect_scores=parsed["aspect_scores"],
        representative_reviews=parsed["representative_reviews"],
        full_text=parsed["full_text"],
    )
