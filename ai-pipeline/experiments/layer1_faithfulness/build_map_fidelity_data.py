#!/usr/bin/env python
"""Layer 1 보강 — Map 단계 충실도(하한) 데이터 결선.

목적: 1층은 reduce 산출물(요약·점수)의 충실도만 봤다. 그 토대인 **로컬 Map(gemma)**이
리뷰에서 근거를 정직하게 뽑는지는 미측정이었다. 이 스크립트는 **저장된 reduce payload**의
Map 출력(=합성된 주장)과 **게임 원문 리뷰 풀**을 짝지어, "Map이 원문에 없는 말을 지어냈는가"를
RAGAS faithfulness로 잴 입력 JSON을 만든다.

- response(채점 대상) = Map이 합성한 텍스트: evidence_items[].detail + critic_signals(praise/criticism)
  + aspects[].pros/cons. (원문 그대로 복사한 quote snippet이 아니라 Map이 **풀어 쓴** 주장 — 환각이
  생길 수 있는 지점.)
- context(근거) = 그 게임의 원문 리뷰 풀(critic_user_data_full20.json의 critic+user contexts, cap).
  주의: review_id 단위 정밀 출처가 아니라 **게임 전체 풀**과 대조 → "어떤 리뷰에도 없는 주장"만
  잡는 **하한(coarse) 측정**. 정밀판(청크 review_id↔원문)은 DB 접근 필요(클라우드).
- LLM 호출 없음(결선만). 채점은 score_map_fidelity.py(venv, Gemini).

입력: artifacts/reduce_payloads/keep/game_{id}_*.json (최신 force_full) + critic_user_data_full20.json
출력: map_fidelity_data.json
실행: python ai-pipeline/experiments/layer1_faithfulness/build_map_fidelity_data.py
"""
from __future__ import annotations

import glob
import json
import os
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
_ROOT = os.path.normpath(os.path.join(_EXP, os.pardir, os.pardir))
KEEP_DIR = os.path.join(_ROOT, "ai-pipeline", "artifacts", "reduce_payloads", "keep")
FULL20 = os.path.join(_THIS, "critic_user_data_full20.json")
OUT = os.path.join(_THIS, "map_fidelity_data.json")

CHUNKS_PER_GAME = int(os.getenv("MAP_FID_CHUNKS", "4"))   # 게임당 채점할 청크 수(claim 많은 순)
CTX_CAP = int(os.getenv("MAP_FID_CTX_CAP", "80"))          # 게임당 근거 리뷰 상한
MIN_CLAIMS = int(os.getenv("MAP_FID_MIN_CLAIMS", "3"))     # 청크 채택 최소 claim 수


def _latest_payload(game_id: int) -> str | None:
    hits = glob.glob(os.path.join(KEEP_DIR, f"game_{game_id}_*.json"))
    return max(hits, key=os.path.getmtime) if hits else None


def _as_dict(chunk):
    return json.loads(chunk) if isinstance(chunk, str) else chunk


def _claims_of(chunk: dict) -> list[str]:
    """Map이 합성한 주장 텍스트만 모은다(원문 복사 snippet 제외)."""
    out: list[str] = []
    for it in chunk.get("evidence_items") or []:
        d = (it.get("detail") or "").strip()
        if d:
            out.append(d)
    cs = chunk.get("critic_signals") or {}
    for key in ("praise", "criticism"):
        for s in cs.get(key) or []:
            s = (s or "").strip()
            if s:
                out.append(s)
    for asp, v in (chunk.get("aspects") or {}).items():
        if not isinstance(v, dict):
            continue
        for key in ("pros", "cons"):
            for s in v.get(key) or []:
                s = (s or "").strip()
                if s:
                    out.append(f"{asp}: {s}")
    # dedup 보존순서
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s); uniq.append(s)
    return uniq


def main() -> int:
    full = {g["game_id"]: g for g in json.load(open(FULL20, encoding="utf-8"))}
    rows = []
    for gid in sorted(full):
        pf = _latest_payload(gid)
        if not pf:
            print(f"  game {gid}: payload 없음 → skip"); continue
        rp = json.load(open(pf, encoding="utf-8"))["reduce_payload"]
        chunks = [_as_dict(c) for c in (rp.get("grouped_summaries") or {}).get("all") or []]

        # 게임 원문 풀(critic+user), dedup, cap
        g = full[gid]
        ctx_all = list(dict.fromkeys((g.get("critic_contexts") or []) + (g.get("user_contexts") or [])))
        ctx = [c for c in ctx_all if (c or "").strip()][:CTX_CAP]
        if not ctx:
            print(f"  game {gid}: context 없음 → skip"); continue

        # claim 많은 청크 우선 채택
        scored = [(c, _claims_of(c)) for c in chunks]
        scored = [(c, cl) for c, cl in scored if len(cl) >= MIN_CLAIMS]
        scored.sort(key=lambda x: len(x[1]), reverse=True)
        picked = scored[:CHUNKS_PER_GAME]
        if not picked:
            print(f"  game {gid}: claim>={MIN_CLAIMS} 청크 없음 → skip"); continue

        for c, claims in picked:
            rows.append({
                "game_id": gid,
                "title": g.get("title", str(gid)),
                "chunk_no": c.get("chunk_no"),
                "n_claims": len(claims),
                "response": " ".join(claims),
                "contexts": ctx,
            })
        print(f"  game {gid:>3} {g.get('title','')[:22]:<22} 청크 {len(picked)}개 "
              f"(claim {[len(cl) for _, cl in picked]}) ctx={len(ctx)}")

    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n총 {len(rows)}개 (게임 {len(set(r['game_id'] for r in rows))}) → {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
