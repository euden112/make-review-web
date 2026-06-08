#!/usr/bin/env python
"""1층 B — RAGAS faithfulness 지표 신뢰도 검증 (mismatched context / negative control).

질문: 헤드라인 faithfulness(0.931)가 실제로 "요약↔근거"의 충실도를 재는가(타당성)?
방법: 요약(response)은 그대로 두고, RAGAS context만 **다른 게임의 근거**로 바꿔치기.
      지표가 grounding을 본다면 faithfulness가 폭락해야 정상. 안 떨어지면 지표가
      근거를 안 본다는 뜻 → 그 사실도 정직하게 보고한다.

설계
  - 입력: 기존 ../../eval_ragas_reduce_result.csv (game_id, user_input, response,
          retrieved_contexts, faithfulness) — 100개분 그대로 재사용(재생성 없음).
  - 표본: ../core_eval_set.csv 의 코어셋 20개.
  - 같은 judge로 한 번에 두 조건 채점(공정한 paired 비교):
      normal     = (response, 자기 근거)        ← 같은 cap·judge로 재채점한 기준선
      mismatched = (response, 다른 게임 근거)    ← negative control
  - 짝짓기: 시드 고정 derangement(고정점 없음) → 각 게임 근거가 자기 자신엔 안 감.
  - context 수는 양 조건 동일하게 cap(--n-contexts)해서 토큰·공정성 통제.
  - Groq 불필요. Gemini(GOOGLE_API_KEY)만 사용.

출력 (이 폴더)
  - mismatched_results.csv : 게임별 orig/normal/mismatched faithfulness + 짝
  (집계·검정·해석은 ANALYSIS.md 'B. mismatched 검증'으로 통합 — 별도 리포트 미생성)

실행 (격리 venv)
  /home/ubuntu/.venv-ragas/bin/python mismatched_control.py
  /home/ubuntu/.venv-ragas/bin/python mismatched_control.py --n-contexts 40 --games 79 12 32
"""
from __future__ import annotations

import argparse
import ast
import csv
import os
import random
import sys

csv.field_size_limit(10 ** 9)

_THIS = os.path.dirname(os.path.abspath(__file__))
_EXP = os.path.dirname(_THIS)                 # experiments/
_AIPIPE = os.path.dirname(_EXP)               # ai-pipeline/
_REPO = os.path.dirname(_AIPIPE)              # repo root
SRC_CSV = os.path.join(_AIPIPE, "eval_ragas_reduce_result.csv")
CORE_CSV = os.path.join(_EXP, "core_eval_set.csv")
OUT_CSV = os.path.join(_THIS, "mismatched_results.csv")

SEED = 20260607


def _load_env() -> None:
    """repo .env에서 GOOGLE_API_KEY 등을 로드(미설정 키만)."""
    path = os.path.join(_REPO, ".env")
    if not os.path.exists(path):
        return
    for line in open(path, encoding="utf-8"):
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _read_core() -> list[tuple[int, str]]:
    out = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _read_src() -> dict[int, dict]:
    rows = {}
    with open(SRC_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            try:
                gid = int(r["game_id"])
            except (TypeError, ValueError):
                continue
            try:
                ctx = ast.literal_eval(r["retrieved_contexts"])
            except (ValueError, SyntaxError):
                ctx = []
            rows[gid] = {
                "user_input": r["user_input"],
                "response": r["response"],
                "contexts": [c for c in ctx if isinstance(c, str) and c.strip()],
                "orig_faith": float(r["faithfulness"]) if r.get("faithfulness") else None,
            }
    return rows


def _derangement(ids: list[int], rng: random.Random) -> dict[int, int]:
    """각 id를 다른 id에 매핑(고정점 없음). 시드 고정."""
    for _ in range(1000):
        shuffled = ids[:]
        rng.shuffle(shuffled)
        if all(a != b for a, b in zip(ids, shuffled)):
            return dict(zip(ids, shuffled))
    raise RuntimeError("derangement 생성 실패")


def _faithfulness_scores(samples: list[dict], judge_model: str) -> list[float]:
    """RAGAS Faithfulness 채점 → 입력 순서대로 점수 리스트."""
    from ragas import EvaluationDataset, evaluate
    from ragas.metrics import Faithfulness
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig
    from langchain_google_genai import ChatGoogleGenerativeAI

    judge = LangchainLLMWrapper(ChatGoogleGenerativeAI(model=judge_model, temperature=0))
    dataset = EvaluationDataset.from_list([
        {"user_input": s["user_input"], "response": s["response"],
         "retrieved_contexts": s["retrieved_contexts"]}
        for s in samples
    ])
    result = evaluate(
        dataset=dataset,
        metrics=[Faithfulness()],
        llm=judge,
        run_config=RunConfig(timeout=180, max_retries=2, max_wait=65, max_workers=1),
    )
    df = result.to_pandas()
    return [float(x) if x == x else float("nan") for x in df["faithfulness"].tolist()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n-contexts", type=int, default=0,
                    help="양 조건 공통 context cap (0=전체, 원본 채점 방법론과 동일)")
    ap.add_argument("--judge-model", default=os.getenv("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite"))
    ap.add_argument("--games", type=int, nargs="*", help="일부 game_id만(테스트)")
    ap.add_argument("--seed", type=int, default=SEED)
    args = ap.parse_args()

    _load_env()
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY 미설정 — .env에 키 필요")
        return 1

    src = _read_src()
    core = _read_core()
    titles = {gid: t for gid, t in core}
    ids = [gid for gid, _ in core if gid in src]
    if args.games:
        want = set(args.games)
        ids = [g for g in ids if g in want]
    if len(ids) < 2:
        print("표본 부족(2개 이상 필요)")
        return 1

    rng = random.Random(args.seed)
    pair = _derangement(ids, rng)  # gid → 근거를 빌려올 다른 gid
    cap = args.n_contexts

    def _cap(ctx: list[str]) -> list[str]:
        return ctx if cap <= 0 else ctx[:cap]

    normal_samples, mism_samples = [], []
    for gid in ids:
        s = src[gid]
        other = pair[gid]
        normal_samples.append({
            "user_input": s["user_input"], "response": s["response"],
            "retrieved_contexts": _cap(s["contexts"]),
        })
        mism_samples.append({
            "user_input": s["user_input"], "response": s["response"],
            "retrieved_contexts": _cap(src[other]["contexts"]),
        })

    cap_label = "전체" if cap <= 0 else str(cap)
    print(f"1층 B mismatched control | 표본 {len(ids)}개 | cap={cap_label} | judge={args.judge_model} | seed={args.seed}")
    print("정상(자기 근거) 채점 중...", flush=True)
    normal = _faithfulness_scores(normal_samples, args.judge_model)
    print("mismatched(다른 게임 근거) 채점 중...", flush=True)
    mism = _faithfulness_scores(mism_samples, args.judge_model)

    # ---- 결과 ----
    recs = []
    for i, gid in enumerate(ids):
        recs.append({
            "game_id": gid,
            "title": titles.get(gid, ""),
            "orig_faith_csv": src[gid]["orig_faith"],
            "normal_faith": round(normal[i], 4) if normal[i] == normal[i] else None,
            "mismatched_faith": round(mism[i], 4) if mism[i] == mism[i] else None,
            "mismatched_from": pair[gid],
            "drop": (round(normal[i] - mism[i], 4)
                     if normal[i] == normal[i] and mism[i] == mism[i] else None),
        })

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)

    valid = [r for r in recs if r["drop"] is not None]
    n = len(valid)
    mean_normal = sum(r["normal_faith"] for r in valid) / n if n else float("nan")
    mean_mism = sum(r["mismatched_faith"] for r in valid) / n if n else float("nan")
    mean_drop = sum(r["drop"] for r in valid) / n if n else float("nan")
    dropped = sum(1 for r in valid if r["drop"] > 0)

    try:
        from scipy.stats import wilcoxon
        stat, p = wilcoxon([r["normal_faith"] for r in valid],
                           [r["mismatched_faith"] for r in valid])
        wilcoxon_line = f"Wilcoxon 부호순위: W={stat:.1f}, p={p:.4g}"
    except Exception as e:
        wilcoxon_line = f"Wilcoxon: 미계산({type(e).__name__})"

    # 집계·검정·해석은 ANALYSIS.md 'B. mismatched 검증'으로 통합(별도 리포트 미생성).
    print(f"\n정상(자기 근거) 평균 {mean_normal:.3f} → mismatched(타 게임 근거) 평균 {mean_mism:.3f} "
          f"(하락 {mean_drop:.3f}, {dropped}/{n} 게임)")
    print(wilcoxon_line)
    print(f"→ {OUT_CSV}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
