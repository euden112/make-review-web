"""
gemma3:1b vs qwen2.5:1.5b Map 단계 벤치마크
실행: python ai-pipeline/benchmark_map_models.py
Ollama가 localhost:11434에서 실행 중이어야 합니다.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time

import httpx

ROOT = os.path.dirname(__file__)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ai_module.map_reduce.map_local import summarize_chunk_with_ollama, _is_valid_map_output

OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
MODELS = ["qwen2.5:1.5b", "qwen3:1.7b", "qwen3:1.7b:no_think"]

SAMPLE_CHUNKS = [
    # 영어 리뷰 청크
    """\
[review_id=1] After 200 hours, still the best FPS out there. The maps are well designed and gunplay feels satisfying.
[review_id=2] Great game but anti-cheat is a joke. Got matched with cheaters 3 times this week.
[review_id=3] Runs perfectly on my mid-range PC. 144fps stable with everything on high.
[review_id=4] The new operation is disappointing. Only 4 maps for $15 is not worth it.
[review_id=5] Competitive matchmaking is broken. Rank system doesn't reflect skill at all.
""",
    # 한국어 리뷰 청크
    """\
[review_id=101] 핵쟁이가 너무 많아서 게임이 망가졌습니다. 안티치트 시스템이 제대로 작동하지 않아요.
[review_id=102] 그래픽은 훌륭하고 최적화도 잘 되어있습니다. 중간 사양 PC에서도 잘 돌아갑니다.
[review_id=103] 매칭 시스템이 엉망입니다. 실력 차이가 너무 나는 팀과 매칭되는 경우가 많습니다.
[review_id=104] 업데이트 이후로 렉이 심해졌어요. 이전에는 이런 문제가 없었는데 최근 패치 이후로 자꾸 끊깁니다.
[review_id=105] 친구들과 함께 즐기기에 최고의 게임입니다. 가격 대비 즐길거리가 매우 많습니다.
""",
    # 영어+한국어 혼합
    """\
[review_id=201] Best competitive shooter available. Movement mechanics feel natural after practice.
[review_id=202] 총기 반동 패턴 익히는 재미가 있어요. 처음엔 어렵지만 익숙해지면 중독성 있습니다.
[review_id=203] Server tick rate was improved in the latest patch. Makes a noticeable difference.
[review_id=204] 최근 업데이트로 맵 밸런스가 개선되었습니다. 특히 미라지 변경이 좋았어요.
[review_id=205] Still having issues with VAC bans being delayed. Cheaters ruin ranked experience.
""",
]

PROMPT_TEMPLATE = (
    "Summarize this game review chunk using the following structure:\n"
    "PROS: up to 4 bullet points (e.g. '- smooth combat system')\n"
    "CONS: up to 4 bullet points (e.g. '- frequent crashes on launch')\n"
    "ASPECTS: (list only aspects actually discussed: graphics / controls / optimization / content / price_value)\n"
    "IDS: (comma-separated review_ids as evidence)\n\n"
    "{chunk}"
)


def _strip_thinking(text: str) -> str:
    """<think>...</think> 블록 제거 후 실제 답변만 반환."""
    import re
    stripped = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    return stripped if stripped else text


async def run_single(client: httpx.AsyncClient, model: str, chunk_no: int, chunk: str) -> dict:
    no_think = model.endswith(":no_think")
    actual_model = model.replace(":no_think", "") if no_think else model
    prefix = "/no_think\n" if no_think else ""
    prompt = prefix + PROMPT_TEMPLATE.format(chunk=chunk)
    start = time.perf_counter()
    try:
        output, input_tokens, output_tokens = await summarize_chunk_with_ollama(
            client=client,
            base_url=OLLAMA_BASE_URL,
            model_name=actual_model,
            prompt=prompt,
            timeout_sec=180,
        )
        if no_think:
            output = _strip_thinking(output)
        elapsed = time.perf_counter() - start
        valid = _is_valid_map_output(output)
        return {
            "chunk_no": chunk_no,
            "model": model,
            "elapsed_sec": round(elapsed, 2),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "valid_format": valid,
            "output": output,
            "error": None,
        }

    except Exception as e:
        elapsed = time.perf_counter() - start
        return {
            "chunk_no": chunk_no,
            "model": model,
            "elapsed_sec": round(elapsed, 2),
            "input_tokens": 0,
            "output_tokens": 0,
            "valid_format": False,
            "output": "",
            "error": str(e),
        }


def print_result(r: dict) -> None:
    status = "PASS" if r["valid_format"] else "FAIL"
    print(f"  [{status}] chunk={r['chunk_no']} | {r['elapsed_sec']}s | "
          f"in={r['input_tokens']} out={r['output_tokens']} tokens")
    if r["error"]:
        print(f"    ERROR: {r['error']}")
    else:
        print(f"    {r['output'][:300].replace(chr(10), ' / ')}")


async def main() -> None:
    print(f"Ollama: {OLLAMA_BASE_URL}")
    print(f"Models: {MODELS}")
    print(f"Chunks: {len(SAMPLE_CHUNKS)}\n")

    all_results: dict[str, list[dict]] = {m: [] for m in MODELS}

    async with httpx.AsyncClient() as client:
        for model in MODELS:
            print(f"{'='*60}")
            print(f"Model: {model}")
            print(f"{'='*60}")
            model_start = time.perf_counter()
            for i, chunk in enumerate(SAMPLE_CHUNKS):
                r = await run_single(client, model, i + 1, chunk)
                all_results[model].append(r)
                print_result(r)
            model_elapsed = time.perf_counter() - model_start
            print(f"  총 소요: {round(model_elapsed, 2)}s\n")

    print(f"\n{'='*60}")
    print("요약 비교")
    print(f"{'='*60}")
    print(f"{'모델':<20} {'성공률':>8} {'평균(s)':>10} {'총 out토큰':>12}")
    print("-" * 54)
    for model in MODELS:
        results = all_results[model]
        passed = sum(1 for r in results if r["valid_format"])
        avg_time = sum(r["elapsed_sec"] for r in results) / len(results)
        total_out = sum(r["output_tokens"] for r in results)
        print(f"{model:<20} {passed}/{len(results):>6} {avg_time:>10.2f} {total_out:>12}")


if __name__ == "__main__":
    asyncio.run(main())
