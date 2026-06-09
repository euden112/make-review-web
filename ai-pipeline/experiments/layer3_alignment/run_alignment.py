#!/usr/bin/env python
"""Layer 3 러너 — 코어셋 점수 정합성 평가 + 실데이터 신뢰도 검증 + 리포트.

데이터 소스(우리 클라우드 상황):
  - 요약 점수: 라이브 API GET /api/v1/games/{id}/summary (sentiment_score + aspect_sentiment).
    → 실제 배포 산출물. 코어셋 20개 전부 커버(payload 불필요). LLM 호출 없음.
  - 원본 리뷰: DB external_reviews (is_recommended + review_categories_json), steam만(추천여부 존재).
  핵심 지표 코드는 score_alignment_eval.py 그대로, 어댑터(load_from_reduce_payload)만 우리 스키마.

실행(컨테이너): docker exec capstone_backend python /workspace/ai-pipeline/experiments/layer3_alignment/run_alignment.py
출력(이 폴더): alignment_results.csv (집계·검정·해석은 ANALYSIS.md로 통합 — 별도 리포트 미생성)
"""
from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import statistics
import sys
import urllib.request
from contextlib import redirect_stdout

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import score_alignment_eval as sae  # 핵심 지표(불변) + 우리 어댑터

from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.domain import ExternalReview

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
OUT_CSV = os.path.join(_THIS, "alignment_results.csv")
API_BASE = os.getenv("ALIGN_API_BASE", "http://localhost:8000")
SEED = 20260607


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _fetch_summary(game_id: int) -> dict | None:
    url = f"{API_BASE}/api/v1/games/{game_id}/summary"
    try:
        with urllib.request.urlopen(url, timeout=30) as resp:
            return json.load(resp)
    except Exception as e:  # noqa: BLE001
        print(f"  summary fetch 실패 game {game_id}: {e}")
        return None


async def _load_review_rows(game_id: int) -> list[dict]:
    """DB external_reviews → [{is_recommended, categories}] (steam, is_recommended not null)."""
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(ExternalReview.is_recommended, ExternalReview.review_categories_json).where(
                ExternalReview.game_id == game_id,
                ExternalReview.is_recommended.isnot(None),
                ExternalReview.is_deleted == False,  # noqa: E712
            )
        )).all()
    out = []
    for is_rec, cats_json in rows:
        cats = [c.get("category") for c in (cats_json or []) if isinstance(c, dict) and c.get("category")]
        out.append({"is_recommended": bool(is_rec), "categories": cats})
    return out


async def main_async() -> int:
    core = _read_core()
    print(f"Layer 3 점수 정합성 | 코어셋 {len(core)}개 | 소스: 라이브 summary + DB reviews | seed {SEED}")

    recs = []
    pooled_reviews: list[sae.Review] = []
    for gid, title in core:
        summary = _fetch_summary(gid)
        rows = await _load_review_rows(gid)
        if not summary or not rows:
            print(f"  SKIP game {gid} (summary/reviews 부족)")
            continue
        reviews, summ = sae.load_from_reduce_payload(summary, rows)
        res = sae.evaluate(reviews, summ)
        pooled_reviews.extend(reviews)
        rec = {
            "game_id": gid, "title": title,
            "total_alignment": res.total.score,
            "category_alignment": res.category.score,
            "macro": res.macro,
            "pos_rate": res.total.pos_rate,
            "score_norm": res.total.score_norm,
            "signed_gap": res.total.signed_gap,
            "coverage": res.category.coverage,
            "n_reviews": res.total.n_reviews,
        }
        for cat in sae.CATEGORIES:
            rec[f"cat_{cat}"] = res.category.per_category.get(cat, "")
        recs.append(rec)
        print(f"  game {gid:>3} {title[:24]:<24} total={res.total.score:.3f} "
              f"cat={res.category.score:.3f} cov={res.category.coverage:.2f}")

    if not recs:
        print("결과 없음")
        return 1

    # ---- CSV ----
    fields = (["game_id", "title", "total_alignment", "category_alignment", "macro",
               "pos_rate", "score_norm", "signed_gap", "coverage", "n_reviews"]
              + [f"cat_{c}" for c in sae.CATEGORIES])
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in recs:
            w.writerow({k: r.get(k, "") for k in fields})

    # ---- 실데이터 신뢰도 검증 (pooled 코어셋 리뷰) ----
    buf = io.StringIO()
    with redirect_stdout(buf):
        pooled_pass = sae.validate(pooled_reviews, seed=SEED)
    # 게임별 통과 수(verbose 억제)
    per_game_pass = 0
    for gid, title in core:
        gr = next((rr for rr in recs if rr["game_id"] == gid), None)
        if not gr:
            continue
        rows = await _load_review_rows(gid)
        reviews = [sae.Review(bool(x["is_recommended"]), x["categories"]) for x in rows]
        if len(reviews) < 2:
            continue
        with redirect_stdout(io.StringIO()):
            if sae.validate(reviews, seed=SEED):
                per_game_pass += 1

    # ---- 집계 (두 축 독립) ----
    n = len(recs)
    mean_total = statistics.fmean(r["total_alignment"] for r in recs)
    mean_cat = statistics.fmean(r["category_alignment"] for r in recs)
    mean_cov = statistics.fmean(r["coverage"] for r in recs)

    # 집계·검정·해석은 ANALYSIS.md로 통합 — 별도 리포트 미생성(CSV만 출력).
    print(f"\n총점 정합 평균 {mean_total:.3f} | 카테고리 정합 평균 {mean_cat:.3f} (cov {mean_cov:.2f})")
    print(f"실데이터 validate: pooled {'통과' if pooled_pass else '실패'}, 게임별 {per_game_pass}/{n}")
    print(f"→ {OUT_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main_async()))
