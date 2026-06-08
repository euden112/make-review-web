#!/usr/bin/env python
"""Layer 1 확장 — 평론가·유저 요약 faithfulness 평가 (단일 통합 스크립트).

§3 1층 헤드라인(통합 요약)을 **출력별로 확장**: 평론가 요약·유저 요약 각각이 자기 근거에
얼마나 충실한지 측정. (구 score_critic_user.py[60-cap] + score_user_full.py[전체근거]를 통합.)

설계
- 요약(response): 라이브 API `/critic-summary`·`/user-summary` (이미 생성됨 → **Groq 호출 0**).
- 근거(context): DB external_reviews 원문을 타입별 분리(평론가=review_type_id 2, 유저=1).
  → fetch_critic_user_data.py 로 미리 수집(JSON). 권장 CTX_CAP=0(전체 근거): 60-cap이 유저 점수를
    부당하게 깎는 것을 확인했으므로 전체 근거가 공정하다.
- judge: Gemini(격리 venv). mismatch(지표 타당성)는 통합에서 입증한 것으로 갈음 — 같은 지표.
- **게임별 1건씩 채점 → 매 게임 후 CSV 저장**(중단돼도 보존). seed 파일이 있으면 그 게임은 건너뜀.

데이터 소스 우선순위(JSON): --in 인자 > critic_user_data_full20.json > critic_user_data.json

실행(venv): /home/ubuntu/.venv-ragas/bin/python eval_critic_user_faith.py --arm both
출력: critic_user_results.csv (또는 --out)

※ 최종 원자료는 critic_user_faithfulness.csv(평론가 + 유저 전체근거), 종합 해석은
   ANALYSIS.md(1층 통합 분석)의 'C. 출력별 확장' 참조. 이 스크립트는 재현/재실행용 단일 도구.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys

_THIS = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_THIS)))
JUDGE_DEFAULT = os.getenv("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite")


def _load_env() -> None:
    p = os.path.join(_REPO, ".env")
    if os.path.exists(p):
        for line in open(p, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def _default_in() -> str:
    for name in ("critic_user_data_full20.json", "critic_user_data.json"):
        p = os.path.join(_THIS, name)
        if os.path.exists(p):
            return p
    return os.path.join(_THIS, "critic_user_data_full20.json")


def _score_one(q: str, resp: str, ctx: list[str], judge_model: str) -> float | None:
    """단일 요약 faithfulness. 실패(한도 소진 등) 시 None."""
    try:
        from ragas import EvaluationDataset, evaluate
        from ragas.metrics import Faithfulness
        from ragas.llms import LangchainLLMWrapper
        from ragas.run_config import RunConfig
        from langchain_google_genai import ChatGoogleGenerativeAI

        judge = LangchainLLMWrapper(ChatGoogleGenerativeAI(model=judge_model, temperature=0))
        ds = EvaluationDataset.from_list([{"user_input": q, "response": resp, "retrieved_contexts": ctx}])
        res = evaluate(dataset=ds, metrics=[Faithfulness()], llm=judge,
                       run_config=RunConfig(timeout=300, max_retries=3, max_wait=65, max_workers=1))
        v = float(res.to_pandas()["faithfulness"].iloc[0])
        return v if v == v else None
    except Exception as e:  # noqa: BLE001
        print(f"    채점 실패: {type(e).__name__}: {str(e)[:120]}", flush=True)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", choices=["critic", "user", "both"], default="both")
    ap.add_argument("--judge-model", default=JUDGE_DEFAULT)
    ap.add_argument("--in", dest="in_json", default=_default_in())
    ap.add_argument("--out", default=os.path.join(_THIS, "critic_user_faithfulness.csv"))
    ap.add_argument("--seed-csv", default=None, help="이미 채점한 결과 CSV(해당 game은 건너뜀)")
    ap.add_argument("--games", type=int, nargs="*")
    a = ap.parse_args()

    _load_env()
    if not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY 미설정")
        return 1
    data = json.load(open(a.in_json, encoding="utf-8"))
    if a.games:
        want = set(a.games)
        data = [d for d in data if d["game_id"] in want]

    arms = ["critic", "user"] if a.arm == "both" else [a.arm]
    fields = ["game_id", "title", "critic_faith", "critic_n_ctx", "user_faith", "user_n_ctx"]

    seed: dict[int, dict] = {}
    if a.seed_csv and os.path.exists(a.seed_csv):
        for r in csv.DictReader(open(a.seed_csv, encoding="utf-8-sig")):
            seed[int(r["game_id"])] = r

    results: dict[int, dict] = {}

    def _write() -> None:
        with open(a.out, "w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=fields)
            w.writeheader()
            for gid in sorted(results):
                w.writerow({k: results[gid].get(k, "") for k in fields})

    print(f"평론가/유저 faith | arm={a.arm} | {len(data)}개 | judge={a.judge_model} | in={os.path.basename(a.in_json)}")
    for i, d in enumerate(data, 1):
        gid = d["game_id"]
        rec = {"game_id": gid, "title": d["title"],
               "critic_faith": "", "critic_n_ctx": len(d["critic_contexts"]),
               "user_faith": "", "user_n_ctx": len(d["user_contexts"])}
        for arm in arms:
            resp, ctx = d[f"{arm}_response"], d[f"{arm}_contexts"]
            if gid in seed and seed[gid].get(f"{arm}_faith"):
                rec[f"{arm}_faith"] = round(float(seed[gid][f"{arm}_faith"]), 4)
                continue
            if not (resp and ctx):
                continue
            v = _score_one(f"{d['title']}의 {arm} 평가는?", resp, ctx, a.judge_model)
            rec[f"{arm}_faith"] = round(v, 4) if v is not None else ""
        results[gid] = rec
        _write()  # ★ 매 게임 후 저장
        print(f"  [{i}/{len(data)}] game {gid} {d['title'][:22]:<22} "
              f"critic={rec['critic_faith']} user={rec['user_faith']}", flush=True)

    def _mean(key):
        vals = [r[key] for r in results.values() if isinstance(r[key], (int, float))]
        return statistics.fmean(vals) if vals else float("nan"), len(vals)

    mc, nc = _mean("critic_faith")
    mu, nu = _mean("user_faith")
    print(f"\n평론가 평균 {mc:.3f}(n={nc}) | 유저 평균 {mu:.3f}(n={nu})")
    print(f"→ {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
