from __future__ import annotations

from functools import lru_cache

from sentence_transformers import SentenceTransformer, util


@lru_cache(maxsize=1)
def _get_model() -> SentenceTransformer:
    return SentenceTransformer("paraphrase-multilingual-MiniLM-L12-v2")


def compute_semantic_similarity(review_texts: list[str], summary_text: str) -> float:
    cleaned_review_texts = [text.strip() for text in review_texts if text and text.strip()]
    cleaned_summary_text = summary_text.strip()
    if not cleaned_review_texts or not cleaned_summary_text:
        return 0.0

    model = _get_model()
    review_embeddings = model.encode(cleaned_review_texts, convert_to_tensor=True)
    summary_embedding = model.encode(cleaned_summary_text, convert_to_tensor=True)
    averaged_review_embedding = review_embeddings.mean(dim=0)
    score = util.cos_sim(averaged_review_embedding, summary_embedding).item()
    score = max(0.0, min(1.0, float(score)))
    return round(score, 4)