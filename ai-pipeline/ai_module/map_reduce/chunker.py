from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Sequence


@dataclass(slots=True)
class Chunk:
    chunk_no: int
    review_ids: list[int]
    text: str


# 1 토큰 ≈ 2.5 문자 (한·영 혼합 기준 보수적 추정)
_CHARS_PER_TOKEN = 2.5

# 청크 크기는 "추출 품질" 기준으로 정한다. 큰 청크 하나를 주면 로컬 모델이
# 청크 내 리뷰를 빠짐없이 evidence로 추출하지 않고 일부만 요약해, evidence가
# 빈약해지고 pros/cons가 부족해진다(검증: 5500자=1청크 → 게이트 미달,
# ~1000자=다청크 → 게이트 통과). 따라서 기본 청크 크기는 추출 친화 목표값으로
# 두고, OLLAMA_NUM_CTX는 입력 truncation을 막는 상한 안전장치로만 쓴다.
_TARGET_CHUNK_CHARS = 1400


def _resolve_max_chars(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    num_ctx_env = os.getenv("OLLAMA_NUM_CTX")
    if num_ctx_env:
        try:
            num_ctx = int(num_ctx_env)
            # JSON Map prompts include schema instructions plus deterministic candidates.
            # num_ctx 상한을 넘으면 입력이 잘리므로, 안전 입력 토큰을 천장으로 삼되
            # 추출 친화 목표값을 넘지 않게 한다.
            safe_input_tokens = max(num_ctx - 1650, 320)
            ceiling = int(safe_input_tokens * _CHARS_PER_TOKEN)
            return min(_TARGET_CHUNK_CHARS, ceiling)
        except ValueError:
            pass
    return _TARGET_CHUNK_CHARS


def chunk_reviews_by_chars(
    review_items: Sequence[tuple[int, str] | tuple[int, str, int | None, float | None]],
    max_chars: int | None = None,
    overlap_reviews: int = 2,
) -> list[Chunk]:
    max_chars = _resolve_max_chars(max_chars)
    chunks: list[Chunk] = []
    buffer_ids: list[int] = []
    buffer_texts: list[str] = []
    cur_len = 0
    chunk_no = 1

    for item in review_items:
        review_id, text = item[0], item[1]
        helpful = item[2] if len(item) > 2 else None
        playtime = item[3] if len(item) > 3 else None

        meta = f"review_id={review_id}"
        if helpful:
            meta += f" helpful={helpful}"
        if playtime is not None and playtime >= 1:
            meta += f" playtime={int(playtime)}h"
        one = f"[{meta}] {text}\n"
        if cur_len + len(one) > max_chars and buffer_ids:
            chunks.append(
                Chunk(
                    chunk_no=chunk_no,
                    review_ids=buffer_ids.copy(),
                    text="".join(buffer_texts),
                )
            )
            chunk_no += 1

            keep = min(overlap_reviews, len(buffer_ids))
            buffer_ids = buffer_ids[-keep:]
            buffer_texts = buffer_texts[-keep:]
            cur_len = sum(len(item) for item in buffer_texts)

        buffer_ids.append(review_id)
        buffer_texts.append(one)
        cur_len += len(one)

    if buffer_ids:
        chunks.append(Chunk(chunk_no=chunk_no, review_ids=buffer_ids, text="".join(buffer_texts)))

    return chunks
