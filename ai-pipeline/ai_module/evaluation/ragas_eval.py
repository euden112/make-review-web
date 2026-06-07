"""RAGAS 기반 단계별 평가 구조 (Map / Reduce 분리).

핵심 설계:
  - 평가 context는 **원문 리뷰 200개**가 아니라 **각 단계가 실제로 참고한 근거**다.
  - Reduce 평가(MVP): 최종 요약(response)이 Reduce 입력 근거(Map evidence /
    대표 인용)에 충실한가 → context = grouped_summaries의 evidence_items detail +
    representative_quotes.
  - Map 평가(향후): 각 evidence가 원문 리뷰에 충실한가 → response = evidence.detail,
    context = 그 리뷰의 snippet/원문. (아래 build_map_rows 스텁 참고.)

이렇게 단계를 분리하면 "Reduce가 근거에 충실한가"와 "Map이 원문에 충실한가"를
독립적으로 측정해, 어느 단계에서 충실도가 깨지는지 가려낼 수 있다.

payload 출처: run_map_pipeline이 첫/force 실행 때 저장하는
ai-pipeline/artifacts/reduce_payloads/keep/game_{id}_*.json 의 reduce_payload.
"""
from __future__ import annotations

import glob
import json
import os

_THIS_DIR = os.path.dirname(__file__)
DEFAULT_PAYLOAD_DIR = os.path.normpath(
    os.path.join(_THIS_DIR, os.pardir, os.pardir, "artifacts", "reduce_payloads", "keep")
)


def find_latest_payload(game_id: int, payload_dir: str | None = None) -> str | None:
    """게임의 가장 최근 reduce payload 파일 경로(없으면 None)."""
    base = payload_dir or DEFAULT_PAYLOAD_DIR
    cands = glob.glob(os.path.join(base, f"game_{game_id}_*.json"))
    if not cands:
        return None
    return max(cands, key=os.path.getmtime)


def _reduce_payload(payload: dict) -> dict:
    return payload.get("reduce_payload", payload)


def extract_reduce_contexts(
    payload: dict,
    groups: tuple[str, ...] = ("all",),
    max_items: int = 400,
) -> list[str]:
    """Reduce가 실제로 받은 근거를 자연어 context 리스트로 추출한다.

    - grouped_summaries[group]의 각 chunk JSON에서 evidence_items의 public_detail
      (없으면 detail)을 뽑는다. 스포일러 마스킹된 public_detail을 우선해 노출 기준과
      맞춘다. (review_id, aspect, 앞부분) 키로 중복 제거.
    - representative_quotes(자연어 대표 인용)도 합친다.
    원문 리뷰 전체가 아니라 'Reduce 입력 근거'만 담으므로 토큰이 작고, 평가 대상이
    "최종 요약 ↔ Reduce 입력 근거"로 정확히 한정된다.
    """
    rp = _reduce_payload(payload)
    gs = rp.get("grouped_summaries") or {}
    seen: set = set()
    ctx: list[str] = []
    for g in groups:
        for raw in gs.get(g, []) or []:
            try:
                chunk = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            for ev in (chunk.get("evidence_items") or []):
                detail = (ev.get("public_detail") or ev.get("detail") or "").strip()
                if not detail:
                    continue
                key = (ev.get("review_id"), ev.get("aspect"), detail[:40])
                if key in seen:
                    continue
                seen.add(key)
                ctx.append(f"[review_id={ev.get('review_id')}] {detail}")
                if len(ctx) >= max_items:
                    break
            if len(ctx) >= max_items:
                break
    for q in (rp.get("representative_quotes") or []):
        if isinstance(q, str) and q.strip():
            ctx.append(q.strip())
    return ctx


def build_reduce_sample(
    game_id: int,
    final_summary: str,
    question: str | None = None,
    payload_dir: str | None = None,
) -> dict | None:
    """Reduce 평가용 RAGAS 샘플 1건.

    response = 최종 요약(final_summary, 호출측이 DB/API에서 가져와 전달),
    retrieved_contexts = Reduce 입력 근거(extract_reduce_contexts).
    """
    path = find_latest_payload(game_id, payload_dir)
    if not path:
        return None
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    contexts = extract_reduce_contexts(payload)
    if not contexts or not (final_summary or "").strip():
        return None
    title = _reduce_payload(payload).get("target_game_title") or f"game {game_id}"
    return {
        "game_id": game_id,
        "question": question or f"{title}의 장단점과 전반적인 평가는?",
        "answer": final_summary,
        "contexts": contexts,
    }


_ASPECT_KO = {
    "graphics": "그래픽", "gameplay": "게임플레이", "story": "스토리", "sound": "사운드",
    "controls": "조작감", "optimization": "최적화", "content": "콘텐츠", "difficulty": "난이도",
    "price_value": "가성비",
}
_POLARITY_KO = {"positive": "긍정적", "negative": "부정적", "mixed": "복합적"}


def _aspect_ko(a: str) -> str:
    return _ASPECT_KO.get((a or "").strip().lower(), a or "리뷰")


def _textualize_map_output(chunk: dict) -> list[str]:
    """chunk의 Map '산출물'(분류·추출)을 검증 가능한 한국어 claim 리스트로.

    원문을 그대로 옮긴 detail/snippet은 제외하고, Map이 *생성*한 것만 담는다:
      - evidence별 aspect+polarity 분류 ("리뷰 N은 게임플레이를 긍정적으로 평가")
      - aspect별 pros/cons 추출, critic 호평/비판 추출
    이 claim들이 원문(context)에 의해 뒷받침되는지가 곧 Map 분류·추출의 충실도.
    """
    claims: list[str] = []
    for ev in (chunk.get("evidence_items") or []):
        rid = ev.get("review_id")
        asp = _aspect_ko(ev.get("aspect"))
        pol = _POLARITY_KO.get((ev.get("polarity") or "").strip().lower())
        if rid is not None and pol:
            claims.append(f"리뷰 {rid}은(는) {asp}을(를) {pol}으로 평가했다.")
    for aspect, av in (chunk.get("aspects") or {}).items():
        ako = _aspect_ko(aspect)
        for pro in (av.get("pros") or []):
            claims.append(f"{ako} 장점: {pro}")
        for con in (av.get("cons") or []):
            claims.append(f"{ako} 단점: {con}")
    # critic 호평/비판 claim은 critic 근거가 있는 chunk에서만(user 전용 chunk에 섞여
    # 부당하게 faithfulness가 깎이는 것 방지).
    if int((chunk.get("source_mix") or {}).get("metacritic_critic", 0) or 0) > 0:
        cs = chunk.get("critic_signals") or {}
        for pr in (cs.get("praise") or []):
            claims.append(f"평론가 호평: {pr}")
        for cr in (cs.get("criticism") or []):
            claims.append(f"평론가 비판: {cr}")
    return claims


def build_map_rows(
    payload: dict,
    groups: tuple[str, ...] = ("all",),
    max_chunks: int = 0,
) -> list[dict]:
    """Map 평가용 RAGAS 행 리스트 (chunk 단위).

    각 chunk:
      retrieved_contexts = 그 chunk의 원문 리뷰(evidence detail/public_detail)
      response(answer)   = Map의 분류·추출 claim(_textualize_map_output)
    → faithfulness = "Map output이 원문 chunk에 충실한가". 오분류(부정→긍정 태깅)·
      원문에 없는 점 추출을 잡아낸다. chunk당 context가 작아(리뷰 수 개) 토큰이 가볍다.
    max_chunks>0이면 chunk 수 상한(빠른 표본 평가용).
    """
    rp = _reduce_payload(payload)
    gs = rp.get("grouped_summaries") or {}
    rows: list[dict] = []
    for g in groups:
        for raw in gs.get(g, []) or []:
            try:
                chunk = json.loads(raw) if isinstance(raw, str) else raw
            except (TypeError, ValueError):
                continue
            contexts = []
            for ev in (chunk.get("evidence_items") or []):
                txt = (ev.get("detail") or ev.get("public_detail") or "").strip()
                if txt:
                    contexts.append(f"[review_id={ev.get('review_id')}] {txt}")
            claims = _textualize_map_output(chunk)
            if not contexts or not claims:
                continue
            rows.append({
                "chunk_no": chunk.get("chunk_no"),
                "question": "이 리뷰들이 평가한 항목과 긍·부정은 무엇인가?",
                "answer": "\n".join(claims),
                "contexts": contexts,
            })
            if max_chunks and len(rows) >= max_chunks:
                return rows
    return rows
