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

# 환경변수 OLLAMA_NUM_CTX 가 설정되면 그 값에서 프롬프트 오버헤드(약 200토큰)와
# 출력 예약(약 400토큰)을 제외한 만큼만 안전 입력 토큰으로 사용한다.
def _resolve_max_chars(explicit: int | None) -> int:
    if explicit is not None:
        return explicit
    num_ctx_env = os.getenv("OLLAMA_NUM_CTX")
    if num_ctx_env:
        try:
            num_ctx = int(num_ctx_env)
            # JSON Map prompts include schema instructions plus deterministic candidates.
            # Keep raw chunks smaller so small local models can return complete JSON.
            safe_input_tokens = max(num_ctx - 1650, 320)
            return int(safe_input_tokens * _CHARS_PER_TOKEN)
        except ValueError:
            pass
    return 5500  # 기본값 (GPU + 큰 num_ctx 가정)


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
