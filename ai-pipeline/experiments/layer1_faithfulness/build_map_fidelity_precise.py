#!/usr/bin/env python
"""Layer 1 보강(정밀) — review_id 기반 Map 충실도 데이터 결선.

전체풀 대조(build_map_fidelity_data.py)는 "게임에 없는 말을 지어냈나"(환각 floor)만 본다.
이 정밀판은 **청크의 실제 출처 review_id 원문만** context로 써서, 오귀속(리뷰 X가 칭찬했다고
했는데 X는 비판)까지 잡는다. coverage-miss = 0(그 청크가 본 리뷰만 대조).

- 선행: fetch_review_texts.py(컨테이너)로 review_texts_full20.json 생성 → 로컬 배치.
- response = Map 합성 주장(evidence_items detail + critic_signals + aspects pros/cons).
- context = 청크 review_ids(∪ evidence_items/quote_candidates review_id)의 원문 텍스트.
- LLM 호출 없음. 채점: MAP_FID_DATA=map_fidelity_precise_data.json score_map_fidelity.py.

출력: map_fidelity_precise_data.json
실행: python ai-pipeline/experiments/layer1_faithfulness/build_map_fidelity_precise.py
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
TEXTS = os.path.join(_THIS, "review_texts_full20.json")
OUT = os.path.join(_THIS, "map_fidelity_precise_data.json")

CHUNKS_PER_GAME = int(os.getenv("MAP_FID_CHUNKS", "4"))
MIN_CLAIMS = int(os.getenv("MAP_FID_MIN_CLAIMS", "3"))


def _latest_payload(game_id: int) -> str | None:
    hits = glob.glob(os.path.join(KEEP_DIR, f"game_{game_id}_*.json"))
    return max(hits, key=os.path.getmtime) if hits else None


def _as_dict(chunk):
    return json.loads(chunk) if isinstance(chunk, str) else chunk


def _claims_of(chunk: dict) -> list[str]:
    out: list[str] = []
    for it in chunk.get("evidence_items") or []:
        d = (it.get("detail") or "").strip()
        if d:
            out.append(d)
    cs = chunk.get("critic_signals") or {}
    for key in ("praise", "criticism"):
        for s in cs.get(key) or []:
            if (s or "").strip():
                out.append(s.strip())
    for asp, v in (chunk.get("aspects") or {}).items():
        if isinstance(v, dict):
            for key in ("pros", "cons"):
                for s in v.get(key) or []:
                    if (s or "").strip():
                        out.append(f"{asp}: {s.strip()}")
    seen, uniq = set(), []
    for s in out:
        if s not in seen:
            seen.add(s); uniq.append(s)
    return uniq


def _chunk_review_ids(chunk: dict) -> list[int]:
    ids = set(chunk.get("review_ids") or [])
    for it in chunk.get("evidence_items") or []:
        if it.get("review_id") is not None:
            ids.add(it["review_id"])
    for q in chunk.get("quote_candidates") or []:
        if q.get("review_id") is not None:
            ids.add(q["review_id"])
    return sorted(ids)


def main() -> int:
    if not os.path.exists(TEXTS):
        print(f"[중단] {TEXTS} 없음 — 먼저 fetch_review_texts.py(컨테이너) 실행 후 로컬 배치.")
        return 1
    id2txt_all = json.load(open(TEXTS, encoding="utf-8"))   # {gid: {rid: text}}
    full = {g["game_id"]: g for g in json.load(open(FULL20, encoding="utf-8"))}

    rows, miss_total, id_total = [], 0, 0
    for gid in sorted(full):
        pf = _latest_payload(gid)
        if not pf:
            print(f"  game {gid}: payload 없음 → skip"); continue
        id2txt = id2txt_all.get(str(gid), {})
        if not id2txt:
            print(f"  game {gid}: 원문 덤프 없음 → skip"); continue
        rp = json.load(open(pf, encoding="utf-8"))["reduce_payload"]
        chunks = [_as_dict(c) for c in (rp.get("grouped_summaries") or {}).get("all") or []]

        scored = [(c, _claims_of(c), _chunk_review_ids(c)) for c in chunks]
        scored = [(c, cl, rids) for c, cl, rids in scored if len(cl) >= MIN_CLAIMS and rids]
        scored.sort(key=lambda x: len(x[1]), reverse=True)
        picked = scored[:CHUNKS_PER_GAME]
        if not picked:
            print(f"  game {gid}: 적격 청크 없음 → skip"); continue

        gmiss, gids = 0, 0
        for c, claims, rids in picked:
            ctx = [id2txt[str(r)] for r in rids if str(r) in id2txt]
            miss = len(rids) - len(ctx)
            gmiss += miss; gids += len(rids)
            if not ctx:
                continue
            rows.append({
                "game_id": gid,
                "title": full[gid].get("title", str(gid)),
                "chunk_no": c.get("chunk_no"),
                "n_claims": len(claims),
                "n_src_reviews": len(ctx),
                "n_missing_src": miss,
                "response": " ".join(claims),
                "contexts": ctx,
            })
        miss_total += gmiss; id_total += gids
        print(f"  game {gid:>3} {full[gid].get('title','')[:22]:<22} 청크 {len(picked)} "
              f"(claim {[len(cl) for _, cl, _ in picked]}) src리뷰결손 {gmiss}/{gids}")

    json.dump(rows, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"\n총 {len(rows)}청크 (게임 {len(set(r['game_id'] for r in rows))}) | "
          f"출처 review_id 결손 {miss_total}/{id_total} "
          f"({miss_total/id_total*100:.1f}% — 0에 가까울수록 정밀)" if id_total else "")
    print(f"→ {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
