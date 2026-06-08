#!/usr/bin/env python
"""Layer 4 — 파이프라인 유저/평론가 요약 저장. **새 요약 없음 = LLM/토큰 0.**

비교 대상이 유저/평론가이므로, 라이브 API의 **유저 요약(`/user-summary`)·평론가 요약
(`/critic-summary`)**을 그대로 불러와 저장한다(이미 생성·DB 저장된 결과물). baseline_summaries.json과
같은 형식 {game_id, title, user_summary, critic_summary}.

출력: pipeline_summaries.json
실행: python fetch_pipeline_summaries.py   (localhost:8000 접근만 필요)
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
OUT_JSON = os.path.join(_THIS, "pipeline_summaries.json")
API_BASE = os.getenv("ALIGN_API_BASE", "http://localhost:8000")


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _summary_text(game_id: int, arm: str) -> str:
    """라이브 유저/평론가 요약(한줄평/summary + 장점 + 단점) 텍스트."""
    ep = "user-summary" if arm == "user" else "critic-summary"
    try:
        with urllib.request.urlopen(f"{API_BASE}/api/v1/games/{game_id}/{ep}", timeout=30) as resp:
            s = json.load(resp)
    except Exception as e:  # noqa: BLE001
        print(f"  fetch 실패 game {game_id}/{ep}: {e}")
        return ""
    if not s:
        return ""
    pros = s.get("pros") or []
    cons = s.get("cons") or []
    return "\n".join(filter(None, [
        s.get("summary") or s.get("one_liner"),
        ("장점: " + " / ".join(pros)) if pros else "",
        ("단점: " + " / ".join(cons)) if cons else "",
    ])).strip()


def main() -> int:
    core = _read_core()
    print(f"파이프라인 유저/평론가 요약 수집(새 요약 없음) | {len(core)}개")
    records = []
    for gid, title in core:
        rec = {
            "game_id": gid, "title": title,
            "user_summary": _summary_text(gid, "user"),
            "critic_summary": _summary_text(gid, "critic"),
        }
        records.append(rec)
        print(f"  game {gid:>3} {title[:26]:<26} user {len(rec['user_summary'])}자 / critic {len(rec['critic_summary'])}자")

    with open(OUT_JSON, "w", encoding="utf-8") as fh:
        json.dump(records, fh, ensure_ascii=False, indent=2)
    print(f"\n{len(records)}개 저장 → {OUT_JSON}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
