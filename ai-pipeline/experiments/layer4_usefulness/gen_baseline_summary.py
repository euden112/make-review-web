#!/usr/bin/env python
"""Layer 4 (1/?) — 파이프라인 없는 단순 Groq 요약(베이스라인), **유저/평론가 분리**.

4층(유용성/선호 비교)의 대조군. 비교 대상이 유저 요약·평론가 요약이므로, 베이스라인도
**유저 리뷰 원문만 / 평론가 리뷰 원문만 따로** 넣어 각각 요약한다(타입 혼합 금지).
파이프라인의 Map-Reduce·근거추출·결정론 점수·redaction 전부 없음 — 한 프롬프트에 통째로.

통제: 최종 모델 = GROQ_MODEL(llama-4-scout), 디코딩 temperature 고정·낮게(0.2). Groq만(게임당 2콜).
근거: DB external_reviews 원문, 타입별(유저=review_type_id 1, 평론가=2). 라이브/DB 무손상(읽기 전용).

출력: baseline_summaries.json — 게임별 user/critic 각각 {baseline_summary, n_reviews, pipeline_summary}.
실행(컨테이너): docker exec capstone_backend python /workspace/ai-pipeline/experiments/layer4_usefulness/gen_baseline_summary.py [game_id ...]
"""
from __future__ import annotations

import argparse
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

csv.field_size_limit(10 ** 9)

from sqlalchemy import select, func
from app.core.database import AsyncSessionLocal
from app.models.domain import ExternalReview
from ai_module.map_reduce.key_rotator import GroqKeyRotator
import groq as _groq

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
_REPO = os.path.dirname(os.path.dirname(_EXP))
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
OUT_JSON = os.path.join(_THIS, "baseline_summaries.json")
API_BASE = os.getenv("ALIGN_API_BASE", "http://localhost:8000")

MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")
REVIEW_CHARS = 400
CAP = int(os.getenv("CAP", "150"))  # 타입별 리뷰 상한. 0/음수 = 전부 포함(무제한)

USER_TYPE_ID = 1
CRITIC_TYPE_ID = 2
ARM_LABEL = {"user": "사용자(유저)", "critic": "전문 평론가"}

# 형식을 지정하지 않는다 — 파이프라인식 구조(한줄평/장단점)를 베이스라인에 주입하지 않기 위해
# "그냥 요약하라"고만 한다(순수 단일 프롬프트 대조군).
SYSTEM = "당신은 게임 리뷰를 요약하는 분석가입니다. 주어진 리뷰만 근거로 한국어로 요약합니다."
USER_TMPL = (
    "다음은 게임 '{title}'에 대한 {who} 리뷰 모음입니다. 이를 종합해 한국어로 요약하세요.\n\n"
    "리뷰:\n{reviews}"
)


def _groq_keys() -> str:
    env_path = os.path.join(_REPO, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("GROQ_API_KEYS=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


async def _reviews_by_type(game_id: int, type_id: int) -> list[str]:
    async with AsyncSessionLocal() as db:
        q = (
            select(ExternalReview.review_text_clean).where(
                ExternalReview.game_id == game_id,
                ExternalReview.review_type_id == type_id,
                ExternalReview.is_deleted == False,  # noqa: E712
                func.length(func.trim(ExternalReview.review_text_clean)) > 0,
            ).order_by(ExternalReview.helpful_count.desc(), ExternalReview.id)
        )
        if CAP > 0:
            q = q.limit(CAP)
        rows = (await db.execute(q)).all()
    return [r[0].strip().replace("\n", " ")[:REVIEW_CHARS] for r in rows if r[0]]


async def _baseline(title: str, arm: str, texts: list[str], rotator: GroqKeyRotator) -> str:
    joined = "\n".join(f"- {t}" for t in texts)
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER_TMPL.format(title=title, who=ARM_LABEL[arm], reviews=joined)},
    ]
    last = None
    for _ in range(max(1, rotator.key_count) * 3):
        try:
            client = rotator.make_client()
            resp = await client.chat.completions.create(
                model=MODEL, messages=messages, temperature=0.2, max_tokens=800,
            )
            return (resp.choices[0].message.content or "").strip()
        except _groq.RateLimitError as e:
            last = e
            print("    [429] 키 로테이션", flush=True)
            rotator.rotate()
        except Exception as e:  # noqa: BLE001
            last = e
            rotator.rotate()
    raise RuntimeError(f"baseline 생성 실패: {last}")


async def main_async(game_ids: list[int], out_path: str, seed_path: str | None) -> int:
    core = _read_core()
    if game_ids:
        want = set(game_ids)
        core = [(g, t) for g, t in core if g in want]

    # 이미 끝낸 게임(seed)은 그대로 보존하고 채점 건너뜀
    seed: dict[int, dict] = {}
    if seed_path and os.path.exists(seed_path):
        for r in json.load(open(seed_path, encoding="utf-8")):
            seed[r["game_id"]] = r

    rotator = GroqKeyRotator.from_key_string(_groq_keys())
    cap_label = "전부(무제한)" if CAP <= 0 else str(CAP)
    records = [seed[g] for g, _ in core if g in seed]   # seed 먼저 포함
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    todo = [(g, t) for g, t in core if g not in seed]
    print(f"베이스라인(유저/평론가 분리) | 전체 {len(core)}개(seed {len(records)} 건너뜀, 채점 {len(todo)}) "
          f"| model={MODEL} | 키 {rotator.key_count}개 | 리뷰 상한 {cap_label} → {os.path.basename(out_path)}")

    for i, (gid, title) in enumerate(todo, 1):
        rec = {"game_id": gid, "title": title}
        counts = {}
        for arm, tid in (("user", USER_TYPE_ID), ("critic", CRITIC_TYPE_ID)):
            texts = await _reviews_by_type(gid, tid)
            counts[arm] = len(texts)
            if texts:
                try:
                    rec[f"{arm}_summary"] = await _baseline(title, arm, texts, rotator)
                except Exception as e:  # noqa: BLE001
                    rec[f"{arm}_summary"] = ""
                    print(f"    {arm} 실패: {e}", flush=True)
            else:
                rec[f"{arm}_summary"] = ""
        records.append(rec)
        with open(out_path, "w", encoding="utf-8") as fh:   # 게임마다 저장(중단 보존)
            json.dump(records, fh, ensure_ascii=False, indent=2)
        print(f"  [{i}/{len(todo)}] game {gid} {title[:22]:<22} "
              f"user(리뷰 {counts['user']}, {len(rec['user_summary'])}자) "
              f"critic(리뷰 {counts['critic']}, {len(rec['critic_summary'])}자)", flush=True)

    print(f"\n총 {len(records)}개(seed 포함) → {out_path}")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("game_ids", type=int, nargs="*")
    ap.add_argument("--out", default=OUT_JSON)
    ap.add_argument("--seed", default=None, help="이미 끝낸 결과 JSON(해당 game 건너뜀)")
    a = ap.parse_args()
    sys.exit(asyncio.run(main_async(a.game_ids, a.out, a.seed)))
