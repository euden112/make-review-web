from __future__ import annotations

import re

SPAM_RULE_VERSION = "v2-longtext-bypass-400"

SPAM_MIN_LENGTH = 15
SPAM_MAX_LENGTH = 5000
SPAM_MIN_WORDS = 5
SPAM_REPEAT_LIMIT = 5
SPAM_UNIQUE_RATIO_THRESHOLD = 0.4
SPAM_UNIQUE_RATIO_BYPASS_LENGTH = 400


def is_spam_review(text: str) -> bool:
    cleaned = (text or "").strip()
    if len(cleaned) < SPAM_MIN_LENGTH or len(cleaned) > SPAM_MAX_LENGTH:
        return True

    words = cleaned.split()
    if len(words) < SPAM_MIN_WORDS:
        return True

    if re.search(rf"(.)\\1{{{SPAM_REPEAT_LIMIT},}}", cleaned):
        return True

    # Long-form reviews are more likely to include intentional repetition.
    if len(cleaned) <= SPAM_UNIQUE_RATIO_BYPASS_LENGTH and len(words) >= 6:
        unique_ratio = len(set(words)) / len(words)
        if unique_ratio < SPAM_UNIQUE_RATIO_THRESHOLD:
            return True

    return False
