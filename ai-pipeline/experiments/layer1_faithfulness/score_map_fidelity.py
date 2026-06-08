#!/usr/bin/env python
"""Layer 1 보강 — Map 충실도(하한) 채점.

build_map_fidelity_data.py가 만든 map_fidelity_data.json을 읽어, 청크별 Map 합성 주장이
게임 원문 리뷰 풀에 지지되는지 RAGAS faithfulness로 채점한다. 채점 로직은 기존
eval_critic_user_faith._score_one(Gemini, temp 0)을 **그대로 재사용**(판정 기준 미변경).

- judge: Gemini(기본 gemini-3.1-flash-lite). RPM 15 가정 → 호출 간 sleep.
- 부분 실행: MAP_FID_LIMIT=N (앞 N청크만), 결과는 청크별로 즉시 CSV에 append(중단 안전).

출력: map_fidelity_results.csv + 콘솔 집계(게임 평균·전체 평균).
실행: .venv-ragas-win311/Scripts/python.exe ai-pipeline/experiments/layer1_faithfulness/score_map_fidelity.py
"""
from __future__ import annotations

import csv
import os
import statistics
import sys
import time

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import json

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_THIS, os.pardir, os.pardir, os.pardir))
sys.path.insert(0, _THIS)  # eval_critic_user_faith import

# .env 로드(GOOGLE_API_KEY 등)
for _ln in open(os.path.join(_ROOT, ".env"), encoding="utf-8"):
    _ln = _ln.strip()
    if _ln and not _ln.startswith("#") and "=" in _ln:
        _k, _v = _ln.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from eval_critic_user_faith import _score_one  # noqa: E402

DATA = os.environ.get("MAP_FID_DATA") or os.path.join(_THIS, "map_fidelity_data.json")
OUT = os.environ.get("MAP_FID_OUT") or os.path.join(_THIS, "map_fidelity_results.csv")
JUDGE = os.environ.get("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite")
SLEEP = float(os.environ.get("MAP_FID_SLEEP", "4.2"))   # RPM~15 → 4.2s 간격
LIMIT = int(os.environ.get("MAP_FID_LIMIT", "0"))        # 0=전체


def main() -> int:
    rows = json.load(open(DATA, encoding="utf-8"))
    if LIMIT > 0:
        rows = rows[:LIMIT]
    print(f"Map 충실도(하한) | judge={JUDGE} | 청크 {len(rows)} | ctx=원문풀 | sleep={SLEEP}s\n")

    fresh = not os.path.exists(OUT)
    fh = open(OUT, "a", encoding="utf-8-sig", newline="")
    w = csv.writer(fh)
    if fresh:
        w.writerow(["game_id", "title", "chunk_no", "n_claims", "faithfulness"])

    per_game: dict[int, list[float]] = {}
    for i, r in enumerate(rows, 1):
        q = f"{r['title']}의 리뷰에서 추출한 주장들은 원문에 근거하는가?"
        v = _score_one(q, r["response"], r["contexts"], JUDGE)
        tag = f"{v:.4f}" if v is not None else "nan"
        print(f"  [{i}/{len(rows)}] game {r['game_id']:>3} {r['title'][:20]:<20} "
              f"chunk {str(r['chunk_no']):>3} claims={r['n_claims']:>2} → {tag}", flush=True)
        w.writerow([r["game_id"], r["title"], r["chunk_no"], r["n_claims"], tag])
        fh.flush()
        if v is not None:
            per_game.setdefault(r["game_id"], []).append(v)
        if i < len(rows):
            time.sleep(SLEEP)
    fh.close()

    print("\n=== 게임별 Map 충실도 ===")
    game_means = []
    for gid in sorted(per_game):
        m = statistics.fmean(per_game[gid])
        game_means.append(m)
        title = next(r["title"] for r in rows if r["game_id"] == gid)
        flag = " ⚠️" if m < 0.7 else ""
        print(f"  game {gid:>3} {title[:24]:<24} {m:.3f} (n={len(per_game[gid])}){flag}")

    allv = [v for vs in per_game.values() for v in vs]
    if allv:
        print(f"\n전체 청크 평균 {statistics.fmean(allv):.3f} (n={len(allv)}) | "
              f"게임평균의 평균 {statistics.fmean(game_means):.3f} | "
              f"중앙값 {statistics.median(allv):.3f} | 최저 {min(allv):.3f}")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
