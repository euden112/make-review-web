"""
점수 정합성 평가 (Score Alignment Evaluation) — Layer 3

요약이 매긴 점수가 원본 리뷰 데이터의 실제 긍·부정 분포와 얼마나 일치하는지 측정.
발표 §3 "정합성(Layer 3)" 증거용.

두 축 (둘 다 0~1 정합도, 같은 척도):
    1. 총점 정합성: 요약 총점(0~5)이 원본 추천율(0~1)과 일치하는가
    2. 카테고리 정합성: 항목별 점수(0~10)가 해당 항목 리뷰의 긍·부정 비율과 일치하는가

★ 이 파일은 우리 클라우드 스키마에 맞게 **어댑터(load_from_reduce_payload)와 설정만** 조정했다.
  핵심 지표 함수(compute_*, evaluate, validate)는 원본 그대로 — 손대지 않음.

우리 환경 적응 요약 (선행 확인 결과)
-----------------------------------
- 요약 점수(총점·항목)는 payload `final_summary` 또는 라이브 API `GET /summary` 둘 다에 있음:
    sentiment_score(0~100) → 총점 0~5,  aspect_scores/aspect_sentiment[key]["score"](0~10) → 항목 점수.
- per-review is_recommended·카테고리 태그는 payload에 없음 → DB `external_reviews`에서 읽는다
    (is_recommended + review_categories_json). DB 카테고리 한글 어휘가 모듈 CATEGORIES와 정확히
    일치(콘텐츠 양·조작감·가성비·그래픽·최적화)하므로 추가 매핑 불필요.
- aspect 키(영문) → 모듈 CATEGORIES(한글) 매핑만 ASPECT_TO_CATEGORY로 둔다.
- LLM 호출 없음(stdlib, 결정론) → Groq/Gemini 토큰 한도와 무관.

중요한 가정 (방어 포인트)
------------------------
- "추천율 p (0~1) ↔ 별점 5p" 선형 매핑 전제. STAR_MAX/CATEGORY_MAX·매핑으로 교체 가능.
- 데이터 없는 카테고리는 임의값 주입 없이 평균에서 제외, coverage로 별도 보고.
"""

from __future__ import annotations

import random
import statistics
from dataclasses import dataclass, field

# ============================================================
# 설정
# ============================================================

STAR_MAX = 5.0       # 총점 만점 (0~5)
CATEGORY_MAX = 10.0  # 카테고리 점수 만점 (0~10)

# 평가 대상 카테고리(파이프라인 산출과 동일하게 유지). DB review_categories_json도 동일 한글 사용.
CATEGORIES = ["콘텐츠 양", "조작감", "가성비", "그래픽", "최적화"]

# 파이프라인 aspect 키(영문) → 모듈 CATEGORIES(한글). 어댑터에서만 사용.
ASPECT_TO_CATEGORY = {
    "content": "콘텐츠 양",
    "controls": "조작감",
    "price_value": "가성비",
    "graphics": "그래픽",
    "optimization": "최적화",
}


# ============================================================
# 데이터 클래스
# ============================================================

@dataclass
class Review:
    """원본 리뷰 단위"""
    is_recommended: bool
    review_categories: list[str] = field(default_factory=list)


@dataclass
class Summary:
    """AI 요약이 매긴 점수 (이 Layer는 점수만 검증하므로 텍스트는 불필요)"""
    total_score: float                      # 총 별점 (0~STAR_MAX)
    category_scores: dict[str, float]       # 카테고리명 -> 점수 (0~CATEGORY_MAX)


# ============================================================
# 결과 클래스
# ============================================================

@dataclass
class TotalAlignmentResult:
    score: float            # 0~1 정합도
    pos_rate: float         # 원본 추천율 0~1
    score_norm: float       # 총점 정규화 0~1
    gap: float              # |pos_rate - score_norm|, 부호 포함은 signed_gap
    signed_gap: float       # score_norm - pos_rate (양수=과대평가, 음수=과소평가)
    n_reviews: int


@dataclass
class CategoryAlignmentResult:
    score: float                        # 데이터 있는 항목들의 macro 평균 정합도
    per_category: dict[str, float]      # 데이터 있는 항목별 정합도
    coverage: float                     # 데이터가 있던 항목 비율 (0~1)
    detail: dict


@dataclass
class AlignmentResult:
    total: TotalAlignmentResult
    category: CategoryAlignmentResult
    macro: float  # 두 축 평균 (둘 다 0~1 동일 척도이므로 의미 있음). 보조 지표.

    def report(self) -> str:
        bar_len = 20

        def bar(v: float) -> str:
            v = max(0.0, min(1.0, v))
            filled = round(v * bar_len)
            return "█" * filled + "░" * (bar_len - filled)

        t, c = self.total, self.category
        over = "과대평가" if t.signed_gap > 0 else ("과소평가" if t.signed_gap < 0 else "일치")
        lines = [
            "=" * 56,
            f"  정합성(평균, 보조)     {bar(self.macro)}  {self.macro:.3f}",
            "=" * 56,
            f"  1. 총점 정합성         {bar(t.score)}  {t.score:.3f}",
            f"     원본 추천율          {t.pos_rate:.3f}  (n={t.n_reviews})",
            f"     총점 정규화          {t.score_norm:.3f}",
            f"     편차                 {t.signed_gap:+.3f}  ({over})",
            "-" * 56,
            f"  2. 카테고리 정합성     {bar(c.score)}  {c.score:.3f}",
            f"     데이터 커버리지      {c.coverage:.3f}",
        ]
        for cat in CATEGORIES:
            if cat in c.per_category:
                d = c.detail[cat]
                lines.append(
                    f"     {cat:<8} {c.per_category[cat]:.3f}  "
                    f"(추천율 {d['cat_pos_rate']:.2f} vs 점수 {d['score_norm']:.2f}, n={d['n']})"
                )
            else:
                lines.append(f"     {cat:<8}  ----  (데이터 없음, 평균 제외)")
        lines.append("=" * 56)
        return "\n".join(lines)


# ============================================================
# 1. 총점 정합성  (원본 그대로 — 수정 금지)
# ============================================================

def compute_total_alignment(reviews: list[Review], summary: Summary) -> TotalAlignmentResult:
    """요약 총점(0~STAR_MAX)이 원본 추천율과 일치하는지. 정합도 = 1 - |추천율 - 총점정규화|"""
    if not reviews:
        return TotalAlignmentResult(0.0, 0.0, 0.0, 0.0, 0.0, 0)

    pos_rate = sum(1 for r in reviews if r.is_recommended) / len(reviews)
    score_norm = summary.total_score / STAR_MAX
    signed_gap = score_norm - pos_rate
    gap = abs(signed_gap)
    score = max(0.0, 1.0 - gap)

    return TotalAlignmentResult(
        score=round(score, 4),
        pos_rate=round(pos_rate, 4),
        score_norm=round(score_norm, 4),
        gap=round(gap, 4),
        signed_gap=round(signed_gap, 4),
        n_reviews=len(reviews),
    )


# ============================================================
# 2. 카테고리 정합성  (원본 그대로 — 수정 금지)
# ============================================================

def compute_category_alignment(reviews: list[Review], summary: Summary) -> CategoryAlignmentResult:
    """
    카테고리별 점수(0~CATEGORY_MAX)가 해당 카테고리 리뷰의 추천율과 일치하는지.
    데이터 없는 카테고리는 임의값을 주입하지 않고 평균에서 제외(coverage로 별도 보고).
    """
    per_category: dict[str, float] = {}
    detail: dict = {}

    for cat in CATEGORIES:
        cat_reviews = [r for r in reviews if cat in r.review_categories]
        if not cat_reviews:
            detail[cat] = {"n": 0, "note": "데이터 없음 → 평균 제외"}
            continue

        cat_pos_rate = sum(1 for r in cat_reviews if r.is_recommended) / len(cat_reviews)
        raw = summary.category_scores.get(cat, 0.0)
        score_norm = raw / CATEGORY_MAX
        consistency = max(0.0, 1.0 - abs(cat_pos_rate - score_norm))

        per_category[cat] = round(consistency, 4)
        detail[cat] = {
            "n": len(cat_reviews),
            "cat_pos_rate": round(cat_pos_rate, 4),
            "score_norm": round(score_norm, 4),
            "raw_score": raw,
        }

    coverage = len(per_category) / len(CATEGORIES)
    score = statistics.fmean(per_category.values()) if per_category else 0.0

    return CategoryAlignmentResult(
        score=round(score, 4),
        per_category=per_category,
        coverage=round(coverage, 4),
        detail=detail,
    )


# ============================================================
# 메인 평가  (원본 그대로 — 수정 금지)
# ============================================================

def evaluate(reviews: list[Review], summary: Summary) -> AlignmentResult:
    total = compute_total_alignment(reviews, summary)
    category = compute_category_alignment(reviews, summary)
    # 두 축 모두 0~1 동일 척도 → 평균이 성립(보조 지표). 발표에선 두 축을 각각 보고 권장.
    macro = round((total.score + category.score) / 2.0, 4)
    return AlignmentResult(total=total, category=category, macro=macro)


# ============================================================
# 파이프라인 데이터 어댑터  ★ 우리 클라우드 스키마에 맞게 수정된 부분 ★
# ============================================================

def load_from_reduce_payload(summary: dict, review_rows: list[dict]) -> tuple[list[Review], Summary]:
    """
    우리 스키마 어댑터 (핵심 지표 코드는 불변, 이 함수만 우리 데이터에 맞춤).

    summary: 요약 점수 dict — payload의 `final_summary` 또는 라이브 API `GET /summary` 응답.
        - summary["sentiment_score"]: 0~100  → 총점 0~STAR_MAX 로 환산
        - summary["aspect_scores"] 또는 summary["aspect_sentiment"]:
              {aspect_key: {"score": 0~10, ...}}  → 항목 점수
    review_rows: DB external_reviews에서 읽은 per-review 데이터(steam, is_recommended not null)
        - [{"is_recommended": bool, "categories": ["콘텐츠 양", "재미", ...]}, ...]
        - DB review_categories_json의 한글 카테고리가 CATEGORIES와 동일 어휘이므로 그대로 사용.
    """
    sentiment = float(summary.get("sentiment_score") or 0.0)            # 0~100 가정
    total_score = sentiment / 100.0 * STAR_MAX

    aspects = summary.get("aspect_scores") or summary.get("aspect_sentiment") or {}
    category_scores: dict[str, float] = {}
    for aspect_key, cat in ASPECT_TO_CATEGORY.items():
        node = aspects.get(aspect_key)
        if isinstance(node, dict) and node.get("score") is not None:
            category_scores[cat] = float(node["score"])
        elif isinstance(node, (int, float)):
            category_scores[cat] = float(node)

    summary_obj = Summary(total_score=total_score, category_scores=category_scores)

    reviews = [
        Review(
            is_recommended=bool(r.get("is_recommended")),
            review_categories=list(r.get("categories", [])),
        )
        for r in review_rows
    ]
    return reviews, summary_obj


# ============================================================
# === 신뢰도(타당성) 검증 하베스트 ===  (원본 그대로 — 수정 금지)
# 이 지표를 §3 메인 증거로 쓰려면 아래 검증이 통과해야 한다.
# ============================================================

def _aligned_summary(reviews: list[Review]) -> Summary:
    """데이터와 '정확히' 맞는 점수를 가진 요약 생성 (정상 조건의 상한)."""
    pos_rate = sum(1 for r in reviews if r.is_recommended) / len(reviews)
    cat_scores = {}
    for cat in CATEGORIES:
        cr = [r for r in reviews if cat in r.review_categories]
        if cr:
            cat_scores[cat] = (sum(1 for r in cr if r.is_recommended) / len(cr)) * CATEGORY_MAX
        else:
            cat_scores[cat] = 0.5 * CATEGORY_MAX
    return Summary(total_score=pos_rate * STAR_MAX, category_scores=cat_scores)


def _adversarial_summary(reviews: list[Review]) -> Summary:
    """데이터에 '최대로 어긋난' 점수 (negative control). 추천율이 높으면 점수를 0으로, 낮으면 만점으로."""
    pos_rate = sum(1 for r in reviews if r.is_recommended) / len(reviews)
    total = 0.0 if pos_rate >= 0.5 else STAR_MAX
    cat_scores = {}
    for cat in CATEGORIES:
        cr = [r for r in reviews if cat in r.review_categories]
        if cr:
            cpr = sum(1 for r in cr if r.is_recommended) / len(cr)
            cat_scores[cat] = 0.0 if cpr >= 0.5 else CATEGORY_MAX
        else:
            cat_scores[cat] = 0.5 * CATEGORY_MAX
    return Summary(total_score=total, category_scores=cat_scores)


def _random_summary(rng: random.Random) -> Summary:
    """무작위 점수 (구분력 확인용)."""
    return Summary(
        total_score=rng.uniform(0, STAR_MAX),
        category_scores={cat: rng.uniform(0, CATEGORY_MAX) for cat in CATEGORIES},
    )


def _shifted_summary(reviews: list[Review], delta: float) -> Summary:
    """정합 요약에서 총점을 정규화 기준 delta 만큼 밀어 어긋남의 '정도'를 만든다(단조성 테스트)."""
    base = _aligned_summary(reviews)
    norm = base.total_score / STAR_MAX
    shifted = min(1.0, max(0.0, norm + delta))
    return Summary(total_score=shifted * STAR_MAX, category_scores=base.category_scores)


def _sample_reviews() -> list[Review]:
    """검증용 합성 리뷰 (mixed 분포). 실제 검증은 코어셋 20~30개로 대체 권장."""
    return [
        Review(True,  ["그래픽"]),
        Review(True,  ["그래픽"]),
        Review(False, ["조작감"]),
        Review(False, ["최적화"]),
        Review(True,  ["조작감"]),
        Review(True,  ["가성비", "콘텐츠 양"]),
        Review(False, ["최적화"]),
        Review(True,  ["콘텐츠 양"]),
        Review(True,  ["가성비"]),
        Review(False, ["조작감"]),
    ]


def validate(reviews: list[Review] | None = None, seed: int = 20260607) -> bool:
    """지표 타당성 4종 검증. 모두 통과해야 §3 메인 증거로 쓸 수 있다."""
    rng = random.Random(seed)
    reviews = reviews or _sample_reviews()

    print("\n" + "#" * 56)
    print("# 점수 정합성 지표 — 신뢰도(타당성) 검증")
    print("#" * 56)

    # 1) sanity: 정합 점수 > 어긋난 점수
    aligned = evaluate(reviews, _aligned_summary(reviews)).macro
    adversarial = evaluate(reviews, _adversarial_summary(reviews)).macro
    print(f"\n[1] sanity        정합={aligned:.3f}  vs  어긋남={adversarial:.3f}")
    ok_sanity = aligned > adversarial

    # 2) negative control: 최대 어긋남 → 정합도 붕괴
    drop = aligned - adversarial
    print(f"[2] negative ctrl 하락폭={drop:.3f}  (어긋난 점수에서 정합도 붕괴해야 함)")
    ok_neg = adversarial < 0.4 and drop > 0.4

    # 3) random control: 무작위 점수 평균 (정합과 어긋남 사이의 중간값으로 수렴해야)
    rand_scores = [evaluate(reviews, _random_summary(rng)).macro for _ in range(200)]
    rand_mean = statistics.fmean(rand_scores)
    print(f"[3] random        무작위 점수 평균={rand_mean:.3f}  "
          f"(정합 {aligned:.2f} > 무작위 {rand_mean:.2f} > 어긋남 {adversarial:.2f})")
    ok_rand = aligned > rand_mean > adversarial

    # 4) monotonicity: 어긋난 정도↑ → 정합도↓ (단조 감소)
    deltas = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5]
    curve = [compute_total_alignment(reviews, _shifted_summary(reviews, d)).score for d in deltas]
    print("[4] monotonic     편차별 총점 정합도:")
    for d, v in zip(deltas, curve):
        print(f"        +{d:.1f} → {v:.3f}")
    ok_mono = all(curve[i] >= curve[i + 1] - 1e-9 for i in range(len(curve) - 1))

    passed = ok_sanity and ok_neg and ok_rand and ok_mono
    print("\n" + "-" * 56)
    print(f"  sanity={ok_sanity}  negctrl={ok_neg}  random={ok_rand}  monotonic={ok_mono}")
    print(f"  => {'✅ 전체 통과 — 지표가 정합/부정합을 구분함' if passed else '❌ 실패'}")
    print("-" * 56)
    return passed


# ============================================================
# 데모
# ============================================================

def _demo() -> None:
    reviews = _sample_reviews()

    print("\n[데모] 데이터와 맞는 요약:")
    print(evaluate(reviews, _aligned_summary(reviews)).report())

    print("\n[데모] 데이터에 어긋난 요약:")
    print(evaluate(reviews, _adversarial_summary(reviews)).report())


if __name__ == "__main__":
    _demo()
    ok = validate()
    raise SystemExit(0 if ok else 1)
