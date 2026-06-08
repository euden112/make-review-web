#!/usr/bin/env python
"""Layer 1 보강(정밀) — 코어20 게임의 review_id→원문 텍스트 덤프.

Map 충실도 정밀 측정용. payload 청크는 review_id만 들고 있고 원문 전체 텍스트가 없어서,
청크의 실제 출처 리뷰를 정확히 대조하려면 id→원문 매핑이 필요하다. 이 스크립트가 그 매핑을
DB에서 **읽기 전용**으로 1회 덤프한다(LLM 호출 없음, 쓰기 없음).

- 대상: critic_user_data_full20.json의 game_id 20개.
- 출력: review_texts_full20.json = {game_id(str): {review_id(str): review_text_clean}}.
- 컨테이너 실행(app·DB 경로 보유):
    docker exec capstone_backend python /workspace/ai-pipeline/experiments/layer1_faithfulness/fetch_review_texts.py
  생성된 review_texts_full20.json을 로컬로 가져와 build_map_fidelity_precise.py에서 사용.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import select
from app.core.database import AsyncSessionLocal
from app.models.domain import ExternalReview

_THIS = os.path.dirname(os.path.abspath(__file__))
FULL20 = os.path.join(_THIS, "critic_user_data_full20.json")
OUT = os.path.join(_THIS, "review_texts_full20.json")
TEXT_CAP = int(os.getenv("REVIEW_TEXT_CAP", "2000"))   # 리뷰당 최대 글자(과대 토큰 방지)


async def main() -> int:
    game_ids = sorted({g["game_id"] for g in json.load(open(FULL20, encoding="utf-8"))})
    result: dict[str, dict[str, str]] = {}
    async with AsyncSessionLocal() as db:
        for gid in game_ids:
            rows = (await db.execute(
                select(ExternalReview.id, ExternalReview.review_text_clean).where(
                    ExternalReview.game_id == gid,
                    ExternalReview.is_deleted == False,  # noqa: E712
                )
            )).all()
            m = {
                str(rid): (txt or "").strip()[:TEXT_CAP]
                for rid, txt in rows if (txt or "").strip()
            }
            result[str(gid)] = m
            print(f"  game {gid:>3}: {len(m)} reviews", flush=True)

    json.dump(result, open(OUT, "w", encoding="utf-8"), ensure_ascii=False)
    total = sum(len(v) for v in result.values())
    print(f"\n총 {total} reviews / {len(result)} games → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
