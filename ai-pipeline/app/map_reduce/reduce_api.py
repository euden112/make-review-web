from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import google.generativeai as genai


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
If there is no evidence for a specific aspect in the input, do not fabricate it; omit that aspect or rate it as neutral.
No markdown, no code fences.
""".strip()


def _safe_parse_json(text: str) -> dict[str, Any]:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    return json.loads(raw)


async def run_reduce_stage(
    *,
    api_key: str,
    model_name: str,
    language_code: str,
    map_summaries: list[str],
    max_items: int = 24,
) -> FinalSummary:
    genai.configure(api_key=api_key)
    model = genai.GenerativeModel(
        model_name=model_name,
        system_instruction=REDUCE_SYSTEM_PROMPT,
    )

    picked = [item[:900] for item in map_summaries[:max_items]]
    user_prompt = (
        f"language={language_code}\n"
        "Integrate map summaries into a final sentiment-aware game review summary.\n"
        "Ensure aspect_scores and representative_reviews are grounded in evidence.\n\n"
        + "\n\n".join([f"[map_{idx+1}] {item}" for idx, item in enumerate(picked)])
    )

    try:
        response = await model.generate_content_async(
            user_prompt,
            generation_config=genai.types.GenerationConfig(
                temperature=0.2,
                response_mime_type="application/json",
            ),
        )

        raw_text = (response.text or "").strip()
        parsed = _safe_parse_json(raw_text)
        return FinalSummary(
            one_liner=parsed["one_liner"],
            aspect_scores=parsed["aspect_scores"],
            representative_reviews=parsed["representative_reviews"],
            full_text=parsed["full_text"],
        )
    except Exception as e:
        return FinalSummary(
            one_liner="요약 생성 중 오류가 발생했습니다.",
            aspect_scores={},
            representative_reviews=[],
            full_text=f"Error: {str(e)}",
        )
