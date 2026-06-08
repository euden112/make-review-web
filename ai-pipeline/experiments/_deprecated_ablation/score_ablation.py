#!/usr/bin/env python
"""실험 2 (2/2) — 파이프라인 vs 단순 베이스라인 채점 + 통계.

gen_baseline.py가 만든 baseline_data.json을 읽어, 두 요약을 **같은 근거(동일 입력
표본)·같은 judge(Gemini)**로 RAGAS Faithfulness 채점하고, 보조 축(스포일러 누출 수)과
함께 paired 통계(Wilcoxon·효과크기)로 비교한다.

faithfulness 단독은 베이스라인이 원문을 베껴 높게 나올 수 있으므로(주의), 스포일러
누출 보조 축으로 설계의 실질 우위를 분명히 한다.

출력(이 폴더): ablation_results.csv, ablation_report.md
실행(venv): /home/ubuntu/.venv-ragas/bin/python score_ablation.py
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_THIS)))
IN_JSON = os.path.join(_THIS, "baseline_data.json")
OUT_CSV = os.path.join(_THIS, "ablation_results.csv")
OUT_MD = os.path.join(_THIS, "ablation_report.md")


def _load_env() -> None:
    p = os.path.join(_REPO, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _score(samples: list[dict], judge_model: str) -> list[float]:
    from ragas import EvaluationDataset, evaluate
    from ragas.metrics import Faithfulness
    from ragas.llms import LangchainLLMWrapper
    from ragas.run_config import RunConfig
    from langchain_google_genai import ChatGoogleGenerativeAI

    judge = LangchainLLMWrapper(ChatGoogleGenerativeAI(model=judge_model, temperature=0))
    ds = EvaluationDataset.from_list([
        {"user_input": s["user_input"], "response": s["response"],
         "retrieved_contexts": s["retrieved_contexts"]}
        for s in samples
    ])
    res = evaluate(dataset=ds, metrics=[Faithfulness()], llm=judge,
                   run_config=RunConfig(timeout=180, max_retries=2, max_wait=65, max_workers=1))
    return [float(x) if x == x else float("nan") for x in res.to_pandas()["faithfulness"].tolist()]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--judge-model", default=os.getenv("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite"))
    ap.add_argument("--games", type=int, nargs="*")
    args = ap.parse_args()

    _load_env()
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY 미설정")
        return 1
    if not os.path.exists(IN_JSON):
        print(f"{IN_JSON} 없음 — 먼저 gen_baseline.py 실행")
        return 1

    data = json.load(open(IN_JSON, encoding="utf-8"))
    if args.games:
        want = set(args.games)
        data = [d for d in data if d["game_id"] in want]
    if not data:
        print("표본 없음")
        return 1

    q = "장단점과 전반적인 평가는?"
    pipe_samples, base_samples = [], []
    for d in data:
        ctx = d["sample_texts"]  # 동일 근거(파이프라인이 추린 표본 원문)
        pipe_samples.append({"user_input": f"{d['title']}의 {q}",
                             "response": d["pipeline_summary"], "retrieved_contexts": ctx})
        base_samples.append({"user_input": f"{d['title']}의 {q}",
                             "response": d["baseline_summary"], "retrieved_contexts": ctx})

    print(f"실험 2 ablation | 표본 {len(data)}개 | judge={args.judge_model} | 근거=동일 입력 표본")
    print("파이프라인 요약 채점 중...", flush=True)
    pipe_f = _score(pipe_samples, args.judge_model)
    print("베이스라인 요약 채점 중...", flush=True)
    base_f = _score(base_samples, args.judge_model)

    recs = []
    for i, d in enumerate(data):
        recs.append({
            "game_id": d["game_id"], "title": d["title"], "sample_size": d["sample_size"],
            "pipeline_faith": round(pipe_f[i], 4) if pipe_f[i] == pipe_f[i] else None,
            "baseline_faith": round(base_f[i], 4) if base_f[i] == base_f[i] else None,
            "faith_delta": (round(pipe_f[i] - base_f[i], 4)
                            if pipe_f[i] == pipe_f[i] and base_f[i] == base_f[i] else None),
            "pipeline_spoilers": d["pipeline_spoiler_count"],
            "baseline_spoilers": d["baseline_spoiler_count"],
            "spoiler_delta": d["baseline_spoiler_count"] - d["pipeline_spoiler_count"],
        })

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        import csv
        w = csv.DictWriter(fh, fieldnames=list(recs[0].keys()))
        w.writeheader()
        w.writerows(recs)

    valid = [r for r in recs if r["faith_delta"] is not None]
    n = len(valid)
    mean_pipe = statistics.mean(r["pipeline_faith"] for r in valid)
    mean_base = statistics.mean(r["baseline_faith"] for r in valid)
    deltas = [r["faith_delta"] for r in valid]
    pipe_better = sum(1 for x in deltas if x > 0)
    base_better = sum(1 for x in deltas if x < 0)
    ties = sum(1 for x in deltas if x == 0)

    # 스포일러 보조 축
    sp_pipe = sum(r["pipeline_spoilers"] for r in recs)
    sp_base = sum(r["baseline_spoilers"] for r in recs)
    sp_games_worse = sum(1 for r in recs if r["spoiler_delta"] > 0)  # 베이스라인이 더 누출

    # 통계
    def _wilcoxon(a, b):
        try:
            from scipy.stats import wilcoxon
            if all(x == y for x, y in zip(a, b)):
                return None, None
            stat, p = wilcoxon(a, b)
            return stat, p
        except Exception:
            return None, None

    fW, fP = _wilcoxon([r["pipeline_faith"] for r in valid], [r["baseline_faith"] for r in valid])
    try:
        dz = statistics.mean(deltas) / statistics.pstdev(deltas) if statistics.pstdev(deltas) else float("nan")
    except statistics.StatisticsError:
        dz = float("nan")
    spW, spP = _wilcoxon([r["pipeline_spoilers"] for r in recs], [r["baseline_spoilers"] for r in recs])

    def fmt(v):
        return "n/a" if v is None else (f"{v:.4g}" if isinstance(v, float) else str(v))

    lines = [
        "# 실험 2 — 파이프라인 vs 단순 베이스라인 (ablation)",
        "",
        f"- 표본: 코어셋 {n}개 | judge: {args.judge_model} | 근거: 동일 입력 표본(파이프라인이 추린 ~200개)",
        f"- 통제: 최종 모델 llama-4-scout 동일 · 입력 표본 동일 · 베이스라인 temp=0.2 고정",
        "",
        "## 1. faithfulness (충실도)",
        "",
        f"- 평균 — 파이프라인 **{mean_pipe:.3f}** vs 베이스라인 **{mean_base:.3f}**",
        f"- 파이프라인 우세 {pipe_better} / 베이스라인 우세 {base_better} / 동률 {ties} (n={n})",
        f"- Wilcoxon: W={fmt(fW)}, p={fmt(fP)} | 효과크기 dz={dz:.3f}",
        "",
        ("> 주의: 베이스라인은 원문을 그대로 베끼는 경향이 있어 faithfulness가 높게 나올 수 있다. "
         "충실도만으로 우열을 논하지 말고 아래 보조 축과 함께 본다."),
        "",
        "## 2. 보조 축 — 스포일러 누출 수 (설계의 redaction 효과)",
        "",
        f"- 총 스포일러 용어 — 파이프라인 **{sp_pipe}** vs 베이스라인 **{sp_base}**",
        f"- 베이스라인이 더 누출한 게임 수: {sp_games_worse}/{len(recs)}",
        f"- Wilcoxon(스포일러): W={fmt(spW)}, p={fmt(spP)}",
        "",
        ("파이프라인은 public_detail redaction으로 보스명·엔딩·반전 등을 가린다. 단순 베이스라인은 "
         "그 장치가 없어 내러티브 게임에서 스포일러가 그대로 노출된다."),
        "",
        "## 게임별",
        "",
        "| id | 게임 | 파이프 faith | 베이스 faith | Δfaith | 파이프 스포 | 베이스 스포 |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in recs:
        lines.append(
            f"| {r['game_id']} | {r['title']} | {fmt(r['pipeline_faith'])} | {fmt(r['baseline_faith'])} | "
            f"{fmt(r['faith_delta'])} | {r['pipeline_spoilers']} | {r['baseline_spoilers']} |"
        )
    open(OUT_MD, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    print(f"\nfaithfulness 파이프 {mean_pipe:.3f} vs 베이스 {mean_base:.3f} "
          f"(우세 {pipe_better}/{base_better}/{ties}, p={fmt(fP)})")
    print(f"스포일러 총합 파이프 {sp_pipe} vs 베이스 {sp_base} (베이스 더누출 {sp_games_worse}게임)")
    print(f"→ {OUT_CSV}\n→ {OUT_MD}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
