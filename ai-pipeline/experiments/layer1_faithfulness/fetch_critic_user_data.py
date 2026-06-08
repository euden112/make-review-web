#!/usr/bin/env python
"""Layer 1 확장 (1/2) — 평론가/유저 요약 + 각자 근거를 수집해 JSON 저장.

목적: 헤드라인 faithfulness(통합 요약 0.931)를 **출력별**로 확장 — 평론가 요약·유저 요약이
각자 자기 근거에 얼마나 충실한지 측정하기 위한 데이터 수집.

- 요약(response): 라이브 API `/critic-summary`, `/user-summary` (이미 생성된 산출물 → Groq 호출 0)
- 근거(context): DB external_reviews 원문 — 평론가=review_type_id=2, 유저=review_type_id=1
  (각 요약을 자기 타입 근거와 짝지음 = matched context)
- LLM 호출 없음(수집만). 채점은 score_critic_user.py(venv, Gemini)에서.

실행(컨테이너): docker exec capstone_backend python /workspace/ai-pipeline/experiments/layer1_faithfulness/fetch_critic_user_data.py
출력: critic_user_data.json
"""
from __future__ import annotations

import asyncio
import csv
import json
import os
import sys
import urllib.request

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from sqlalchemy import select, func
from app.core.database import AsyncSessionLocal
from app.models.domain import ExternalReview

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
OUT_JSON = os.path.join(_THIS, "critic_user_data.json")
API_BASE = os.getenv("ALIGN_API_BASE", "http://localhost:8000")

CRITIC_TYPE_ID = 2   # review_types: 2=critic
USER_TYPE_ID = 1     # 1=user
CTX_CAP = int(os.getenv("CTX_CAP", "60"))   # 근거 리뷰 상한. 0/음수 = 전부 포함(무제한)
REVIEW_CHARS = 500   # 리뷰당 길이 컷


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _fetch(path: str) -> dict | None:
    try:
        with urllib.request.urlopen(f"{API_BASE}{path}", timeout=30) as resp:
            return json.load(resp)
    except Exception:  # noqa: BLE001
        return None


def _response_text(s: dict | None) -> str:
    if not s:
        return ""
    pros = s.get("pros") or []
    cons = s.get("cons") or []
    return "\n".join(filter(None, [
        s.get("summary") or s.get("one_liner"),
        ("장점: " + " / ".join(pros)) if pros else "",
        ("단점: " + " / ".join(cons)) if cons else "",
    ])).strip()


async def _contexts(game_id: int, review_type_id: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        q = (
            select(ExternalReview.review_text_clean)
            .where(
                ExternalReview.game_id == game_id,
                ExternalReview.review_type_id == review_type_id,
                ExternalReview.is_deleted == False,  # noqa: E712
                func.length(func.trim(ExternalReview.review_text_clean)) > 0,
            )
            .order_by(ExternalReview.helpful_count.desc(), ExternalReview.id)
        )
        if CTX_CAP > 0:
            q = q.limit(CTX_CAP)
        rows = (await db.execute(q)).all()
    return [r[0].strip().replace("\n", " ")[:REVIEW_CHARS] for r in rows if r[0]]


async def main_async(game_ids: list[int] | None, out_path: str) -> int:
    core = _read_core()
    if game_ids:
        want = set(game_ids)
        core = [(g, t) for g, t in core if g in want]
    cap_label = "전부(무제한)" if CTX_CAP <= 0 else str(CTX_CAP)
    print(f"평론가/유저 데이터 수집 | {len(core)}개 | 근거 cap {cap_label} → {os.path.basename(out_path)}")
    records = []
    for gid, title in core:
        critic_s = _fetch(f"/api/v1/games/{gid}/critic-summary")
        user_s = _fetch(f"/api/v1/games/{gid}/user-summary")
        critic_ctx = await _contexts(gid, CRITIC_TYPE_ID)
        user_ctx = await _contexts(gid, USER_TYPE_ID)
        rec = {
            "game_id": gid, "title": title,
            "critic_response": _response_text(critic_s),
            "critic_contexts": critic_ctx,
            "user_response": _response_text(user_s),
            "user_contexts": user_ctx,
        }
        records.append(rec)
        print(f"  game {gid:>3} {title[:22]:<22} "
              f"critic(resp={'Y' if rec['critic_response'] else '-'}, ctx={len(critic_ctx)}) "
              f"user(resp={'Y' if rec['user_response'] else '-'}, ctx={len(user_ctx)})", flush=True)

    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    print(f"\n{len(records)}개 저장 → {out_path}")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("game_ids", type=int, nargs="*", help="일부 game_id만(생략 시 코어셋 전체)")
    ap.add_argument("--out", default=OUT_JSON, help="출력 JSON 경로")
    a = ap.parse_args()
    raise SystemExit(asyncio.run(main_async(a.game_ids or None, a.out)))
