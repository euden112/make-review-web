from __future__ import annotations

import re

SPAM_RULE_VERSION = "v3-stricter-cpu"

SPAM_MIN_LENGTH = 30  # 15 → 30 (1B 모델은 짧은 입력에서 형식 무너짐)
SPAM_MAX_LENGTH = 5000
SPAM_MIN_WORDS = 6
SPAM_REPEAT_LIMIT = 5
SPAM_UNIQUE_RATIO_THRESHOLD = 0.4
SPAM_UNIQUE_RATIO_BYPASS_LENGTH = 400
SPAM_PUNCT_RATIO_THRESHOLD = 0.4  # 비-알파벳·숫자 비율 상한
SPAM_URL_RATIO_THRESHOLD = 0.3    # URL/링크 점유율 상한

_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_ALNUM_RE = re.compile(r"[\w가-힣]", re.UNICODE)


def is_spam_review(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < SPAM_MIN_LENGTH or len(cleaned) > SPAM_MAX_LENGTH:
        return True

    words = cleaned.split()
    if len(words) < SPAM_MIN_WORDS:
        return True

    if re.search(rf"(.)\1{{{SPAM_REPEAT_LIMIT},}}", cleaned):
        return True

    # URL이 본문의 30% 이상 차지하면 광고/스팸으로 간주
    url_chars = sum(len(m.group(0)) for m in _URL_RE.finditer(cleaned))
    if url_chars / max(len(cleaned), 1) >= SPAM_URL_RATIO_THRESHOLD:
        return True

    # 알파벳·한글·숫자가 60% 미만이면 키스매시/이모지 도배
    alnum_chars = sum(1 for _ in _ALNUM_RE.finditer(cleaned))
    if alnum_chars / max(len(cleaned), 1) < (1 - SPAM_PUNCT_RATIO_THRESHOLD):
        return True

    # Long-form reviews are more likely to include intentional repetition.
    if len(cleaned) <= SPAM_UNIQUE_RATIO_BYPASS_LENGTH and len(words) >= 6:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < SPAM_UNIQUE_RATIO_THRESHOLD:
            return True

    return False
