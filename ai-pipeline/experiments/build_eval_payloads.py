#!/usr/bin/env python
"""0단계 — 코어셋 reduce payload 오프라인 캡처 (라이브 DB·요약 무손상).

요약 엔드포인트(force) 대신 파이프라인을 직접 호출해, reduce 직전 payload와
그때 생성된 최종 요약을 가로채 디스크에 저장한다. DB에는 아무것도 쓰지 않는다.

- 입력: experiments/core_eval_set.csv 의 game_id (인자로 일부만 지정 가능)
- Map 백엔드: groq (GPU 없음). 키는 GROQ_API_KEYS 로테이션.
- 출력 artifact: experiments/payloads/game_{id}_*.json
    포맷 = {artifact_meta, reduce_payload, final_summary}
- 실행: 백엔드 컨테이너 안에서.
    docker exec capstone_backend python /workspace/ai-pipeline/experiments/build_eval_payloads.py
    docker exec capstone_backend python /workspace/ai-pipeline/experiments/build_eval_payloads.py 79   # 단일 게임 테스트
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from ai_module.map_reduce.pipeline import run_hybrid_summary_pipeline, MAP_PROMPT_VERSION
import dry_quality_run as dq  # 리뷰 로더/비율/앵커 재사용 (import만, 수정 없음)

_THIS = os.path.dirname(os.path.abspath(__file__))
CORE_CSV = os.path.join(_THIS, "core_eval_set.csv")
KEEP_DIR = os.path.join(_THIS, "payloads")  # 실험 산출물은 experiments/ 안에 보관
LOG_CSV = os.path.join(_THIS, "payload_build_log.csv")
REVIEW_LIMIT = int(os.getenv("EVAL_REVIEW_LIMIT", "5000"))  # 전체 로드 후 파이프라인이 ~200 샘플


def _groq_keys() -> str:
    """레포 .env의 GROQ_API_KEYS를 우선 사용(컨테이너 재시작 없이 키 추가 반영)."""
    repo = os.path.dirname(os.path.dirname(_THIS))  # ai-pipeline/experiments → repo root
    env_path = os.path.join(repo, ".env")
    if os.path.exists(env_path):
        for line in open(env_path, encoding="utf-8"):
            line = line.strip()
            if line.startswith("GROQ_API_KEYS=") and line.split("=", 1)[1].strip():
                return line.split("=", 1)[1].strip()
    return os.getenv("GROQ_API_KEYS") or os.getenv("GROQ_API_KEY", "")


def _read_core() -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    with open(CORE_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            out.append((int(r["game_id"]), r["title"]))
    return out


def _serialize_summary(fs) -> dict:
    for kw in ({"mode": "json"}, {}):
        try:
            return fs.model_dump(**kw)
        except Exception:
            continue
    return {
        "one_liner": getattr(fs, "one_liner", None),
        "pros": getattr(fs, "pros", None),
        "cons": getattr(fs, "cons", None),
        "aspect_scores": getattr(fs, "aspect_scores", None),
        "sentiment_score": getattr(fs, "sentiment_score", None),
    }


async def build_one(game_id: int, title: str) -> dict:
    reviews = await dq._load_reviews(game_id, REVIEW_LIMIT)
    if not reviews:
        return {"game_id": game_id, "title": title, "status": "no_reviews"}

    box: dict[str, dict] = {}

    def _hook(payload: dict) -> None:
        box["payload"] = dict(payload)

    map_results, final, _buckets = await run_hybrid_summary_pipeline(
        game_id=game_id,
        language_code="ko",
        all_reviews=reviews,
        steam_ratio=dq._steam_ratio(reviews),
        metacritic_ratio=dq._metacritic_ratio(reviews),
        score_anchors=dq._score_anchors(reviews),
        cache=dq.InMemoryAsyncCache(),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        local_model_name=os.getenv("LOCAL_MAP_MODEL", "gemma4:e4b"),
        reduce_api_key=_groq_keys(),
        reduce_model_name=os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        target_game_title=title,
        map_backend=(os.getenv("MAP_BACKEND") or "groq").strip().lower(),
        groq_map_model=os.getenv("GROQ_MAP_MODEL") or os.getenv("GROQ_MODEL"),
        groq_map_api_key=_groq_keys(),
        reduce_payload_hook=_hook,
    )

    payload = box.get("payload")
    if not payload:
        return {"game_id": game_id, "title": title, "status": "no_payload"}

    os.makedirs(KEEP_DIR, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(
        KEEP_DIR, f"game_{game_id}_evalcapture_na-na_groq_{MAP_PROMPT_VERSION}_{ts}.json"
    )
    artifact = {
        "artifact_meta": {
            "game_id": game_id,
            "title": title,
            "saved_at": ts,
            "save_reason": "eval_offline_capture",
            "map_route": "groq",
            "map_prompt_version": MAP_PROMPT_VERSION,
            "retention": "keep",
            "source": "experiments/build_eval_payloads.py",
        },
        "reduce_payload": payload,
        "final_summary": _serialize_summary(final),
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(artifact, fh, ensure_ascii=False, indent=2)

    map_in = sum(int(getattr(m, "input_tokens", 0) or 0) for m in map_results)
    map_out = sum(int(getattr(m, "output_tokens", 0) or 0) for m in map_results)
    return {
        "game_id": game_id,
        "title": title,
        "status": "ok",
        "input_reviews": len(reviews),
        "chunks": len(map_results),
        "map_tokens_in": map_in,
        "map_tokens_out": map_out,
        "one_liner": getattr(final, "one_liner", "")[:80],
        "path": os.path.basename(path),
    }


async def main_async(game_ids: list[int]) -> int:
    core = _read_core()
    if game_ids:
        wanted = set(game_ids)
        core = [(gid, t) for gid, t in core if gid in wanted]
    print(f"대상 {len(core)}개 게임, Map=groq, review_limit={REVIEW_LIMIT}")
    print(f"출력 → {KEEP_DIR}\n")

    rows: list[dict] = []
    for i, (gid, title) in enumerate(core, 1):
        print(f"[{i}/{len(core)}] game {gid} — {title} ...", flush=True)
        try:
            r = await build_one(gid, title)
        except Exception as e:
            r = {"game_id": gid, "title": title, "status": f"ERROR: {type(e).__name__}: {e}"}
        rows.append(r)
        print(f"    → {r.get('status')} "
              f"{'chunks=' + str(r.get('chunks')) if r.get('status') == 'ok' else ''} "
              f"{r.get('path', '')}", flush=True)

    fields = ["game_id", "title", "status", "input_reviews", "chunks",
              "map_tokens_in", "map_tokens_out", "one_liner", "path"]
    with open(LOG_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fields})

    ok = sum(1 for r in rows if r.get("status") == "ok")
    print(f"\n완료: {ok}/{len(rows)} 성공 → 로그 {LOG_CSV}")
    return 0 if ok == len(rows) else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("game_ids", type=int, nargs="*", help="일부 game_id만 (생략 시 코어셋 전체)")
    args = ap.parse_args()
    return asyncio.run(main_async(args.game_ids))


if __name__ == "__main__":
    sys.exit(main())
