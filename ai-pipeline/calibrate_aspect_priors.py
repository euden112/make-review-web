#!/usr/bin/env python
"""aspect별 mention-polarity prior를 저장된 요약 데이터로 실측 산출한다.

reduce_api._ASPECT_POLARITY_PRIOR의 도메인 추정 초기값을, 실제 코퍼스에서 관측된
'평균적 게임의 aspect skew'로 교체·검증하기 위한 보정 도구다.

방법:
  - game_review_summaries(is_current, unified)의 aspect_sentiment_json에서 aspect별
    polarity_mix(positive/mixed/negative)를 읽는다.
  - 게임별 aspect skew = (pos - neg) / (pos + neg + 1) 를 구하고(최소 근거 수 이상만),
    게임 간 평균을 낸다. 이 평균이 그 aspect의 '기댓 skew' = prior 추정치다.
  - 인기작이 평균을 좌우하지 않도록 게임 단위로 동일 가중(게임별 skew의 평균).

실행(백엔드 컨테이너에서 DB 접근):
  docker exec capstone_backend python /workspace/ai-pipeline/calibrate_aspect_priors.py
  docker exec capstone_backend python /workspace/ai-pipeline/calibrate_aspect_priors.py --min-evidence 5

출력의 dict를 reduce_api._ASPECT_POLARITY_PRIOR에 붙여넣어 튜닝한다.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import defaultdict

from sqlalchemy import and_
from sqlalchemy.future import select

from app.core.database import AsyncSessionLocal
from app.models.domain import GameReviewSummary


def _skew(pos: int, neg: int) -> float:
    return (pos - neg) / (pos + neg + 1)


async def main(min_evidence: int) -> int:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(
            select(GameReviewSummary.aspect_sentiment_json).where(
                and_(
                    GameReviewSummary.summary_type == "unified",
                    GameReviewSummary.review_language.is_(None),
                    GameReviewSummary.is_current == True,
                )
            )
        )).scalars().all()

    per_aspect_skews: dict[str, list[float]] = defaultdict(list)
    games_seen = 0
    for aspects in rows:
        if not isinstance(aspects, dict):
            continue
        games_seen += 1
        for aspect, d in aspects.items():
            if not isinstance(d, dict):
                continue
            mix = d.get("polarity_mix")
            if not isinstance(mix, dict):
                continue
            pos = int(mix.get("positive", 0) or 0)
            mxd = int(mix.get("mixed", 0) or 0)
            neg = int(mix.get("negative", 0) or 0)
            if pos + mxd + neg < min_evidence:
                continue
            per_aspect_skews[str(aspect).strip().lower()].append(_skew(pos, neg))

    if not per_aspect_skews:
        print(f"집계 가능한 aspect 데이터 없음 (games={games_seen}, min_evidence={min_evidence})")
        return 1

    print(f"분석 게임 수: {games_seen} / 최소 근거 수: {min_evidence}\n")
    print(f"{'aspect':<14}{'games':>6}{'mean_skew':>11}{'median':>9}")
    print("-" * 40)
    suggested: dict[str, float] = {}
    for aspect in sorted(per_aspect_skews, key=lambda a: sum(per_aspect_skews[a]) / len(per_aspect_skews[a])):
        vals = sorted(per_aspect_skews[aspect])
        n = len(vals)
        mean = sum(vals) / n
        median = vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2
        suggested[aspect] = round(mean, 2)
        print(f"{aspect:<14}{n:>6}{mean:>11.3f}{median:>9.3f}")

    print("\n# reduce_api._ASPECT_POLARITY_PRIOR 에 붙여넣어 튜닝 (실측 mean_skew):")
    print("_ASPECT_POLARITY_PRIOR = {")
    for aspect, val in sorted(suggested.items(), key=lambda kv: kv[1]):
        print(f'    "{aspect}": {val},')
    print("}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-evidence", type=int, default=3,
                    help="게임-aspect당 최소 polarity_mix 근거 수(미만은 표본에서 제외)")
    args = ap.parse_args()
    raise SystemExit(asyncio.run(main(args.min_evidence)))
