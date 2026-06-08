#!/usr/bin/env python
"""2층 — 결정성/재현성: 같은 payload로 reduce를 N회 반복해 점수 분산 측정.

주장: "점수는 코드가 결정하고 LLM은 검증된 delta만 제안한다 → 같은 입력에 같은 점수."
검증: 저장된 reduce_payload(Map 결과 고정)를 그대로 run_feature_reduce_stage에 N회 넣어
      sentiment_score(0–100)·aspect 점수(0–10)의 표준편차/분산을 측정. Map 재실행 없음.

- 입력: experiments/payloads/game_{id}_*.json (build_eval_payloads.py 산출, 최신본)
- 반복: 게임당 REDUCE_REPLAYS회(기본 6)
- 대상: 인자 미지정 시 확정된 5게임(DEFAULT_GAMES)만. 다른 게임은 인자로 명시.
- 출력(이 폴더): determinism_matrix.csv(게임×런 점수), determinism_summary.csv
  (집계·검정·해석은 ANALYSIS.md로 통합 — 별도 리포트 미생성)

실행(컨테이너):
  docker exec capstone_backend python /workspace/ai-pipeline/experiments/layer2_determinism/replay_determinism.py
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import json
import os
import statistics
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ai_module.map_reduce.reduce_api import run_feature_reduce_stage  # import만(수정 없음)

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)
PAYLOAD_DIR = os.path.join(_EXP, "payloads")
OUT_MATRIX = os.path.join(_THIS, "determinism_matrix.csv")
OUT_SUMMARY = os.path.join(_THIS, "determinism_summary.csv")

# 2층 확정 표본(5게임). 인자 미지정 시 이 게임들만 리플레이한다.
DEFAULT_GAMES = [11, 12, 20, 32, 79]

REPLAYS = int(os.getenv("REDUCE_REPLAYS", "6"))
MODEL = os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct")


def _groq_keys() -> str:
    """레포 .env의 GROQ_API_KEYS를 우선 사용(컨테이너 재시작 없이 키 추가 반영)."""
    repo = os.path.dirname(os.path.dirname(_EXP))  # layer2_determinism→experiments→ai-pipeline→repo
    env_path = os.path.join(repo, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("GROQ_API_KEYS=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")


API_KEY = _groq_keys()


def _latest_payloads(game_ids: list[int] | None) -> dict[int, str]:
    out: dict[int, str] = {}
    for path in sorted(glob.glob(os.path.join(PAYLOAD_DIR, "game_*.json"))):
        base = os.path.basename(path)
        try:
            gid = int(base.split("_")[1])
        except (IndexError, ValueError):
            continue
        if game_ids and gid not in game_ids:
            continue
        out[gid] = path  # sorted → 최신(타임스탬프 큰) 것이 마지막에 남음
    return out


def _clean_payload(rp: dict) -> dict:
    rp = dict(rp)
    # JSON 직렬화로 tuple → list 가 된 category_frequency 복원
    cf = rp.get("category_frequency")
    if isinstance(cf, list):
        rp["category_frequency"] = [tuple(x) if isinstance(x, list) else x for x in cf]
    return rp


def _aspect_scores(final) -> dict[str, float]:
    out = {}
    for a, d in (getattr(final, "aspect_scores", None) or {}).items():
        if isinstance(d, dict) and d.get("score") is not None:
            out[a] = float(d["score"])
        elif isinstance(d, (int, float)):
            out[a] = float(d)
    return out


async def _replay_game(gid: int, title: str, rp: dict) -> dict:
    runs = []  # [(sentiment, {aspect:score})]
    for i in range(REPLAYS):
        final = await run_feature_reduce_stage(api_key=API_KEY, model_name=MODEL, **rp)
        sent = getattr(final, "sentiment_score", None)
        runs.append((float(sent) if sent is not None else None, _aspect_scores(final)))
        print(f"    run {i+1}/{REPLAYS}: sentiment={sent}", flush=True)
    return {"game_id": gid, "title": title, "runs": runs}


def _std(vals: list[float]) -> float:
    vals = [v for v in vals if v is not None]
    return statistics.pstdev(vals) if len(vals) > 1 else 0.0


def _write_outputs(results: list[dict]):
    """현재까지 results로 matrix·summary CSV 2종을 (재)기록. (sent_std_mean, asp_std_mean, perfect_sent, perfect_asp, n) 반환.
    집계·검정·해석은 ANALYSIS.md로 통합 — 별도 리포트 미생성."""
    all_aspects = sorted({a for r in results for _, asp in r["runs"] for a in asp})
    with open(OUT_MATRIX, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["game_id", "title", "run", "sentiment_score"] + [f"aspect_{a}" for a in all_aspects])
        for r in results:
            for i, (sent, asp) in enumerate(r["runs"], 1):
                w.writerow([r["game_id"], r["title"], i, sent] + [asp.get(a, "") for a in all_aspects])

    summ_rows = []
    for r in results:
        sents = [s for s, _ in r["runs"]]
        asp_stds = []
        for a in all_aspects:
            col = [asp.get(a) for _, asp in r["runs"] if a in asp]
            if len(col) > 1:
                asp_stds.append(_std(col))
        summ_rows.append({
            "game_id": r["game_id"], "title": r["title"], "runs": len(r["runs"]),
            "sentiment_mean": round(statistics.mean([s for s in sents if s is not None]), 2) if any(s is not None for s in sents) else None,
            "sentiment_std": round(_std(sents), 3),
            "aspect_std_mean": round(statistics.mean(asp_stds), 3) if asp_stds else 0.0,
            "aspect_std_max": round(max(asp_stds), 3) if asp_stds else 0.0,
        })
    with open(OUT_SUMMARY, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(summ_rows[0].keys()))
        w.writeheader()
        w.writerows(summ_rows)

    n = len(summ_rows)
    sent_std_mean = statistics.mean(r["sentiment_std"] for r in summ_rows) if n else 0
    asp_std_mean = statistics.mean(r["aspect_std_mean"] for r in summ_rows) if n else 0
    perfect_sent = sum(1 for r in summ_rows if r["sentiment_std"] == 0)
    perfect_asp = sum(1 for r in summ_rows if r["aspect_std_max"] == 0)
    return sent_std_mean, asp_std_mean, perfect_sent, perfect_asp, n


async def main_async(game_ids: list[int] | None) -> int:
    payloads = _latest_payloads(game_ids)
    if not payloads:
        print(f"payload 없음 ({PAYLOAD_DIR}) — 먼저 build_eval_payloads.py 실행")
        return 1
    print(f"대상 {len(payloads)}개 게임 | 게임당 reduce {REPLAYS}회 | model={MODEL}")

    results = []
    for gid, path in payloads.items():
        art = json.load(open(path, encoding="utf-8"))
        title = art.get("artifact_meta", {}).get("title", f"game {gid}")
        rp = _clean_payload(art["reduce_payload"])
        print(f"[game {gid}] {title}", flush=True)
        try:
            results.append(await _replay_game(gid, title, rp))
            _write_outputs(results)  # 게임마다 즉시 저장 → 중단돼도 완료분 보존
            print("    (저장됨)", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"    ERROR: {type(e).__name__}: {e}", flush=True)

    if not results:
        print("결과 없음 — 모든 게임 실패")
        return 1
    sent_std_mean, asp_std_mean, perfect_sent, perfect_asp, n = _write_outputs(results)
    print(f"\nsentiment std 평균 {sent_std_mean:.3f} | aspect std 평균 {asp_std_mean:.3f} "
          f"| 점수불변 {perfect_sent}/{n}(sentiment), {perfect_asp}/{n}(aspect)")
    print(f"→ {OUT_MATRIX}\n→ {OUT_SUMMARY}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_ids", type=int, nargs="*",
                    help=f"리플레이할 game_id. 미지정 시 확정 5게임 {DEFAULT_GAMES}")
    args = ap.parse_args()
    return asyncio.run(main_async(args.game_ids or DEFAULT_GAMES))


if __name__ == "__main__":
    sys.exit(main())
