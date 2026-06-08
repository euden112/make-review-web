#!/usr/bin/env python
"""실험 2 (1/2) — 단순 베이스라인 요약 생성 + 동일 표본 재현 + 스포일러 수.

ablation: "같은 모델(llama-4-scout)·같은 입력 표본 하에 파이프라인 설계 유무" 비교.
베이스라인 = 파이프라인이 추린 동일 리뷰 표본을 **한 프롬프트에 통째로** 넣어 요약하는
단순 경로(Map-Reduce·근거추출·결정론 점수·스포일러 redaction 전부 없음).

통제(파이프라인과 동일하게)
  - 최종 생성 모델: GROQ_MODEL(llama-4-scout) — 파이프라인 reduce와 동일
  - 입력 리뷰: 파이프라인과 동일한 stratified 표본(약 200개) — 같은 sampler 재현
  - 디코딩: temperature 고정·낮게(0.2)

라이브 무손상: DB 읽기 전용, 요약 테이블·Redis 미접근.
출력: baseline_data.json (게임별 sample_texts / baseline_summary / pipeline_summary / 스포일러 수)

실행(컨테이너): docker exec capstone_backend python /workspace/ai-pipeline/experiments/exp2_ablation/gen_baseline.py [game_id ...]
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

csv.field_size_limit(10 ** 9)

# 기존 코드는 import만 (수정 없음)
import dry_quality_run as dq
from ai_module.map_reduce.pipeline import _normalize_reviews, _summary_review_target
from ai_module.map_reduce.sampler import stratified_select_reviews
from ai_module.map_reduce.map_schema import _spoiler_terms_from_text
from ai_module.map_reduce.key_rotator import GroqKeyRotator
import groq as _groq

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
_AIPIPE = os.path.dirname(_EXP)
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
EVAL_CSV = os.path.join(_AIPIPE, "eval_ragas_reduce_result.csv")
OUT_JSON = os.path.join(_THIS, "baseline_data.json")

MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
REVIEW_CHARS = 400      # 베이스라인 프롬프트에 넣을 리뷰당 길이 컷
MAX_PROMPT_REVIEWS = 200

SYSTEM = "당신은 게임 사용자 리뷰를 요약하는 분석가입니다. 주어진 리뷰만 근거로 한국어로 간결히 요약합니다."
USER_TMPL = (
    "다음은 게임 '{title}'의 사용자 리뷰 모음입니다. 이를 종합해 아래 형식으로 한국어 요약을 작성하세요.\n"
    "형식:\n한줄평: <한 문장>\n장점: <항목1> / <항목2> / <항목3>\n단점: <항목1> / <항목2> / <항목3>\n\n"
    "리뷰:\n{reviews}"
)


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _read_pipeline_summaries() -> dict[int, str]:
    out = {}
    with open(EVAL_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            if r.get("game_id"):
                out[int(r["game_id"])] = r["response"]
    return out


def _text(r) -> str:
    return (getattr(r, "review_text_clean", None) or "").strip()


def _reproduce_sample(reviews) -> list[str]:
    """파이프라인과 동일한 stratified 표본을 재현해 리뷰 텍스트 리스트로 반환."""
    normalized = _normalize_reviews(reviews, "ko")
    selected = stratified_select_reviews(
        normalized,
        steam_ratio=dq._steam_ratio(reviews),
        metacritic_bin_ratio=dq._metacritic_ratio(reviews),
        total_target=_summary_review_target(),
    )
    return [t for t in (_text(r) for r in selected) if t]


async def _baseline_summary(title: str, sample_texts: list[str], rotator: GroqKeyRotator) -> str:
    joined = "\n".join(f"- {t[:REVIEW_CHARS]}" for t in sample_texts[:MAX_PROMPT_REVIEWS])
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(title=title, reviews=joined)},
    ]
    last_err = None
    for _ in range(max(1, rotator.key_count) * 3):
        try:
            client = rotator.make_client()
            resp = await client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0.2, max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
        except _groq.RateLimitError as e:
            last_err = e
            print("    [429] 키 로테이션", flush=True)
            rotator.rotate()
        except Exception as e:  # noqa: BLE001
            last_err = e
            rotator.rotate()
    raise RuntimeError(f"baseline 생성 실패: {last_err}")


async def main_async(game_ids: list[int]) -> int:
    core = _read_core()
    if game_ids:
        want = set(game_ids)
        core = [(g, t) for g, t in core if g in want]
    pipe = _read_pipeline_summaries()
    rotator = GroqKeyRotator.from_key_string(os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", ""))

    print(f"대상 {len(core)}개 | model={MODEL}")
    records = []
    for i, (gid, title) in enumerate(core, 1):
        print(f"[{i}/{len(core)}] game {gid} — {title}", flush=True)
        reviews = await dq._load_reviews(gid, 5000)
        sample = _reproduce_sample(reviews)
        pipeline_summary = pipe.get(gid, "")
        try:
            baseline_summary = await _baseline_summary(title, sample, rotator)
        except Exception as e:  # noqa: BLE001
            print(f"    ERROR: {e}", flush=True)
            continue
        b_sp = _spoiler_terms_from_text(baseline_summary)
        p_sp = _spoiler_terms_from_text(pipeline_summary)
        records.append({
            "game_id": gid, "title": title,
            "sample_size": len(sample),
            "sample_texts": [t[:REVIEW_CHARS] for t in sample],
            "pipeline_summary": pipeline_summary,
            "baseline_summary": baseline_summary,
            "pipeline_spoiler_terms": p_sp,
            "baseline_spoiler_terms": b_sp,
            "pipeline_spoiler_count": len(p_sp),
            "baseline_spoiler_count": len(b_sp),
        })
        print(f"    표본 {len(sample)} | 스포일러 파이프라인 {len(p_sp)} vs 베이스라인 {len(b_sp)}", flush=True)

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    print(f"\n{len(records)}개 저장 → {OUT_JSON}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_ids", type=int, nargs="*")
    args = ap.parse_args()
    return asyncio.run(main_async(args.game_ids))


if __name__ == "__main__":
    sys.exit(main())
