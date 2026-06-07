#!/usr/bin/env python
"""RAGAS faithfulness / answer_relevancy로 요약 품질을 외부 표준 지표로 평가한다.

요약 파이프라인은 "리뷰(근거) → 요약(출력)"이라 RAG 평가가 그대로 맞는다.
 - faithfulness     : 요약의 각 주장이 근거(리뷰)로 뒷받침되는 비율 (환각의 역수)
 - answer_relevancy : 요약이 질문(게임 평가)에 얼마나 적합한지

judge는 요약을 만든 모델(Groq llama-4-scout)이 아니라 **독립 모델 Gemini**를 써서
자기채점 편향을 피한다.

준비 (최신 ragas 0.2+ 기준):
  python -m venv .venv-ragas && . .venv-ragas/bin/activate   # 별도 venv 권장(무거운 deps)
  pip install ragas datasets langchain-google-genai
  # .env에 CLOUD_URL, API_SECRET_KEY, GOOGLE_API_KEY 필요
  # judge/임베딩 모델은 RAGAS_JUDGE_MODEL / RAGAS_EMB_MODEL로 교체 가능
  #   (가용 모델은 Google AI Studio에서 확인: gemini-1.5-flash, gemini-2.0-flash 등)

실행:
  python ai-pipeline/eval_ragas_faithfulness.py                # 기본 표본 게임
  python ai-pipeline/eval_ragas_faithfulness.py 1 2 3 8 12     # 특정 game_id
  결과: 콘솔 표 + eval_ragas_result.csv 저장
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import importlib.util

# --- .env 로드 (CLOUD_URL / API_SECRET_KEY / GOOGLE_API_KEY) ---
_ENV_PATH = os.path.join(os.path.dirname(__file__), os.pardir, ".env")
if os.path.exists(_ENV_PATH):
    for _line in open(_ENV_PATH, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BASE = os.environ["CLOUD_URL"].rstrip("/")
API_KEY = os.environ.get("API_SECRET_KEY", "")
UA = {"User-Agent": "ragas-eval"}

# 평가 표본(기본): 품질 스펙트럼을 고루 포함
DEFAULT_GAMES = [1, 2, 3, 5, 8, 12, 26, 30, 55, 75, 94, 97]
# 근거 리뷰 상한. 0이면 무제한(요약이 본 ~200개 전부 사용 → 컷에 의한 가짜 미지지 제거).
# 전부 넣으면 faithfulness가 더 공정해지나, 무료 티어 토큰 한도(TPM)에 더 자주 걸려 느려진다.
MAX_CONTEXTS = int(os.environ.get("EVAL_MAX_CONTEXTS", "30"))
MAX_REVIEW_CHARS = int(os.environ.get("EVAL_MAX_REVIEW_CHARS", "400"))  # 리뷰당 길이 컷(raw 모드)
# 평가 context 출처: reduce(MVP, 기본) = Reduce가 실제 본 근거(Map evidence/대표 인용),
# raw = 원문 리뷰 전체(대조·디버그용). reduce가 평가 대상("최종 요약 ↔ Reduce 입력 근거")에 정확.
EVAL_SOURCE = os.environ.get("EVAL_SOURCE", "reduce").lower()
# 평가 단계: reduce(최종요약↔Reduce근거) | map(Map분류·추출↔원문 chunk). map은 chunk 단위 행.
EVAL_STAGE = os.environ.get("EVAL_STAGE", "reduce").lower()
MAP_MAX_CHUNKS = int(os.environ.get("EVAL_MAP_MAX_CHUNKS", "0"))  # map: 게임당 chunk 상한(0=전체)
EVAL_METRICS = os.environ.get("EVAL_METRICS", "faithfulness").lower()


def _load_ragas_eval_module():
    """Load ragas_eval.py without importing ai_module.evaluation.__init__."""
    path = os.path.join(os.path.dirname(__file__), "ai_module", "evaluation", "ragas_eval.py")
    spec = importlib.util.spec_from_file_location("_ragas_eval_local", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"ragas_eval.py 로드 실패: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _get(path: str, auth: bool = False) -> dict:
    headers = dict(UA)
    if auth and API_KEY:
        headers["X-API-Key"] = API_KEY
    req = urllib.request.Request(BASE + path, headers=headers)
    return json.load(urllib.request.urlopen(req, timeout=60))


def _final_summary_text(summ: dict) -> str:
    """API 요약에서 평가 대상 응답(최종 요약 텍스트)을 구성."""
    pros = summ.get("pros") or []
    cons = summ.get("cons") or []
    return "\n".join(filter(None, [
        summ.get("one_liner"),
        ("장점: " + " / ".join(pros)) if pros else "",
        ("단점: " + " / ".join(cons)) if cons else "",
    ])).strip()


def build_sample(game_id: int) -> dict | None:
    """게임 1개에서 (question, answer, contexts)를 만든다.

    EVAL_SOURCE=reduce(기본): context = Reduce가 실제 본 근거(Map evidence/대표 인용,
      저장된 reduce payload에서 추출). 평가 대상 = "최종 요약 ↔ Reduce 입력 근거".
    EVAL_SOURCE=raw: context = 원문 리뷰 전체(대조·디버그용).
    """
    summ = _get(f"/api/v1/games/{game_id}/summary")
    answer = _final_summary_text(summ)
    if not answer:
        return None

    if EVAL_SOURCE == "reduce":
        return _load_ragas_eval_module().build_reduce_sample(game_id, answer)

    # raw 모드: 원문 리뷰를 context로
    try:
        title = next(g["canonical_title"] for g in _get("/api/v1/games/") if g["id"] == game_id)
    except Exception:
        title = f"game {game_id}"
    rm = _get(f"/api/v1/games/{game_id}/reviews-for-map?force=true", auth=True)
    texts = []
    for r in (rm.get("reviews") or []):
        t = (r.get("review_text_clean") or "").strip().replace("\n", " ")
        if t:
            texts.append(t[:MAX_REVIEW_CHARS])
        if MAX_CONTEXTS and len(texts) >= MAX_CONTEXTS:
            break
    if not texts:
        return None

    return {
        "game_id": game_id,
        "question": f"{title}의 장단점과 전반적인 평가는?",
        "answer": answer,
        "contexts": texts,
    }


def main(game_ids: list[int]) -> int:
    provider = os.environ.get("RAGAS_LLM_PROVIDER", "gemini").lower()
    if provider == "groq" and not os.environ.get("GROQ_API_KEY"):
        print("GROQ_API_KEY 미설정 — Groq judge에 필요합니다.")
        return 1
    if provider != "groq" and not os.environ.get("GOOGLE_API_KEY"):
        print("GOOGLE_API_KEY 미설정 — Gemini judge에 필요합니다.")
        return 1

    # 최신 ragas(0.2+) API: EvaluationDataset + 클래스형 메트릭
    from ragas import EvaluationDataset, evaluate
    from ragas.metrics import Faithfulness, ResponseRelevancy
    from ragas.llms import LangchainLLMWrapper
    from ragas.embeddings import LangchainEmbeddingsWrapper
    from ragas.run_config import RunConfig

    rows = []
    for gid in game_ids:
        try:
            if EVAL_STAGE == "map":
                ragas_eval = _load_ragas_eval_module()
                p = ragas_eval.find_latest_payload(gid)
                if not p:
                    print(f"  SKIP game {gid} (payload 없음)")
                    continue
                with open(p, encoding="utf-8") as fh:
                    payload = json.load(fh)
                grows = ragas_eval.build_map_rows(payload, max_chunks=MAP_MAX_CHUNKS)
                for r in grows:
                    r["game_id"] = gid
                rows.extend(grows)
                print(f"  표본 준비 game {gid}: chunk {len(grows)}개")
            else:
                s = build_sample(gid)
                if s:
                    rows.append(s)
                    print(f"  표본 준비 game {gid}: 근거 {len(s['contexts'])}건")
                else:
                    print(f"  SKIP game {gid} (요약/근거 부족)")
        except Exception as e:
            print(f"  ERR game {gid}: {e}")

    if not rows:
        print("평가할 표본 없음")
        return 1

    dataset = EvaluationDataset.from_list([
        {"user_input": r["question"], "response": r["answer"], "retrieved_contexts": r["contexts"]}
        for r in rows
    ])

    # judge 제공자. 평가는 faithfulness 중심(요청 절감). answer_relevancy는
    # EVAL_ANSWER_RELEVANCY=1일 때만(gemini, 임베딩 필요). 요약 생성 모델은 자기채점
    # 편향이라 judge로 쓰지 않는다 — 독립 모델만.
    want_ar = os.environ.get("EVAL_ANSWER_RELEVANCY", "0") == "1" or EVAL_METRICS in {"all", "answer_relevancy", "relevancy"}
    if provider == "groq":
        from langchain_groq import ChatGroq
        judge_model = os.environ.get("RAGAS_JUDGE_MODEL", "groq/compound-mini")
        judge = LangchainLLMWrapper(ChatGroq(model=judge_model, temperature=0))
        emb = None
        metrics = [Faithfulness()]   # Groq는 임베딩 미제공
    else:
        from langchain_google_genai import ChatGoogleGenerativeAI, GoogleGenerativeAIEmbeddings
        judge_model = os.environ.get("RAGAS_JUDGE_MODEL", "gemini-3.1-flash-lite")
        judge = LangchainLLMWrapper(ChatGoogleGenerativeAI(model=judge_model, temperature=0))
        emb = None
        metrics = [] if EVAL_METRICS in {"answer_relevancy", "relevancy"} else [Faithfulness()]
        if want_ar:
            emb_model = os.environ.get("RAGAS_EMB_MODEL", "models/gemini-embedding-001")
            emb = LangchainEmbeddingsWrapper(GoogleGenerativeAIEmbeddings(model=emb_model))
            strictness = int(os.environ.get("RAGAS_RELEVANCY_STRICTNESS", "3"))
            metrics.append(ResponseRelevancy(strictness=strictness))

    metric_names = ", ".join(getattr(m, "name", type(m).__name__) for m in metrics)
    print(f"\nRAGAS 평가 시작 (stage={EVAL_STAGE}, 표본 {len(rows)}건, provider={provider}, judge={judge_model}, metrics={metric_names})...")
    result = evaluate(
        dataset=dataset,
        metrics=metrics,
        llm=judge,
        embeddings=emb,
        run_config=RunConfig(timeout=180, max_retries=1, max_wait=65, max_workers=1),
    )
    print("\n=== 집계 ===")
    print(result)

    df = result.to_pandas()
    df.insert(0, "game_id", [r["game_id"] for r in rows])
    if any("chunk_no" in r for r in rows):
        df.insert(1, "chunk_no", [r.get("chunk_no") for r in rows])
    out = os.path.join(os.path.dirname(__file__), f"eval_ragas_{EVAL_STAGE}_result.csv")
    df.to_csv(out, index=False, encoding="utf-8-sig")
    print(f"\n행별 결과 → {out}")
    cols = [c for c in ("game_id", "chunk_no", "faithfulness", "answer_relevancy") if c in df.columns]
    print(df[cols].to_string(index=False))
    return 0


if __name__ == "__main__":
    ids = [int(a) for a in sys.argv[1:]] or DEFAULT_GAMES
    raise SystemExit(main(ids))
