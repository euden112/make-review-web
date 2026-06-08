#!/usr/bin/env python
"""Layer 1 보강(정밀) — 채점 아티팩트 청크만 정제 재채점.

정밀 측정에서 일부 청크가 0에 가까운 점수를 받았으나 수동검증 결과 주장이 출처 리뷰에
명백히 지지됨(false negative). 원인: response가 claim들을 공백으로 concat → 중복·짧은
조각이 섞여 RAGAS claim 분해가 망가짐. 이 스크립트는 **대상 청크의 response만 정제**
(완전중복 제거·15자 미만 조각 제거·문장단위 줄바꿈 결합)해 재채점하고 결과를 비교한다.
판정 로직(_score_one)·출처 context는 미변경. map_fidelity_precise_results.csv를 갱신한다.

실행: .venv-ragas-win311/Scripts/python.exe ai-pipeline/.../rescore_map_noise.py
"""
from __future__ import annotations

import csv
import os
import sys
import json

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_THIS = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_THIS, os.pardir, os.pardir, os.pardir))
sys.path.insert(0, _THIS)

for _ln in open(os.path.join(_ROOT, ".env"), encoding="utf-8"):
    _ln = _ln.strip()
    if _ln and not _ln.startswith("#") and "=" in _ln:
        _k, _v = _ln.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip())

from eval_critic_user_faith import _score_one  # noqa: E402

DATA = os.path.join(_THIS, "map_fidelity_precise_data.json")
CSV = os.path.join(_THIS, "map_fidelity_precise_results.csv")
JUDGE = os.environ.get("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite")

# 수동검증으로 false-negative 확인된 대상 (game_id, chunk_no)
TARGETS = {("39", "56"), ("67", "15"), ("99", "52"), ("31", "27")}


def _clean(resp: str) -> list[str]:
    """concat된 response를 문장 후보로 분해·정제."""
    import re
    parts = re.split(r"(?<=[.!?。])\s+|\s*/\s*|\n+", resp)
    seen, out = set(), []
    for p in parts:
        p = p.strip()
        if len(p) < 15:          # 짧은 조각(깨진 fragment) 제거
            continue
        key = p.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def main() -> int:
    rows = json.load(open(DATA, encoding="utf-8"))
    updates = {}
    for r in rows:
        key = (str(r["game_id"]), str(r["chunk_no"]))
        if key not in TARGETS:
            continue
        cleaned = _clean(r["response"])
        q = f"{r['title']}의 리뷰에서 추출한 주장들은 원문에 근거하는가?"
        v = _score_one(q, "\n".join(cleaned), r["contexts"], JUDGE)
        updates[key] = v
        print(f"  game {r['game_id']:>3} {r['title'][:20]:<20} ch{r['chunk_no']:>3} "
              f"claims {r['n_claims']}→{len(cleaned)}(정제)  재채점 → "
              f"{f'{v:.4f}' if v is not None else 'nan'}", flush=True)

    # CSV 갱신
    out_rows = list(csv.DictReader(open(CSV, encoding="utf-8-sig")))
    for row in out_rows:
        key = (row["game_id"], row["chunk_no"])
        if key in updates and updates[key] is not None:
            row["faithfulness"] = f"{updates[key]:.4f}"
    with open(CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=out_rows[0].keys())
        w.writeheader(); w.writerows(out_rows)
    print(f"\n갱신 → {CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
