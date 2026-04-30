from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(slots=True)
class Chunk:
    chunk_no: int
    review_ids: list[int]
    text: str


def chunk_reviews_by_chars(
    review_items: Sequence[tuple[int, str] | tuple[int, str, int | None, float | None]],
    max_chars: int = 5500,
    overlap_reviews: int = 2,
) -> list[Chunk]:
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
