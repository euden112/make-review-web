"""
리뷰 정제 파이프라인
적용 필터: 길이 / 무의미 텍스트 / 언어
"""

import json
import re
from collections import Counter
from pathlib import Path


# ── 설정값 ───────────────────────────
MIN_LEN        = 50      # 최소 글자 수
MAX_LEN        = 2000    # 상한 초과 시 제거 대신 트런케이션
TRUNCATE_AT    = 2000    # 트런케이션 기준 



# ── 필터 1: 길이 ────────────────────────
def filter_length(body: str, min_len: int = MIN_LEN) -> bool:
    return len(body) >= min_len


# ── 필터 2: 무의미 텍스트(스팸) ─────────────────
def filter_spam(body: str) -> bool:
    """
    아래 패턴에 해당하면 제거:
      - 단일 문자 반복 비율 40% 초과  (예: 'jefeeeeeeee...')
      - 동일 문자 8회 이상 연속       (예: '........')
    """
    text = body.strip()

    # 반복 문자 비율
    no_space = text.replace(' ', '').replace('\n', '')
    if len(no_space) > 5:
        most_common = max(Counter(no_space).values())
        if most_common / len(no_space) > 0.4:
            return False

    # 연속 반복 문자
    if re.search(r'(.)\1{7,}', text):
        return False

    return True


# ── 필터 3: 언어 (영어 전용) ───────────────────
from langdetect import detect, LangDetectException

def filter_language(body: str) -> bool:
    """
    langdetect 라이브러리로 영어 여부 판별.
    감지 실패 시(텍스트가 너무 짧거나 판별 불가) False 반환.
    """
    try:
        return detect(body) == 'en'
    except LangDetectException:
        return False


# ── 트런케이션 ──────────────────────────
def truncate(body: str, max_len: int = TRUNCATE_AT) -> str:
    if len(body) <= max_len:
        return body
    cut = body[:max_len]
    last_stop = max(cut.rfind('. '), cut.rfind('\n'))
    if last_stop > max_len * 0.7:   # 적어도 70% 이상 남기기
        return cut[:last_stop + 1].strip()
    return cut.strip()


# ── 파이프라인 ──────────────────────────
def run_filter(reviews: list[dict]) -> list[dict]:
    """리뷰 리스트를 받아 정제된 리스트를 반환."""
    passed = []
    for r in reviews:
        body = r['body']

        if not filter_length(body):
            continue
        if not filter_spam(body):
            continue
        if not filter_language(body):
            continue

        r = dict(r)  # 원본 변경 방지
        r['body'] = truncate(body)
        passed.append(r)

    return passed


# ── 메인 ─────────────────────────────
if __name__ == '__main__':
    input_path  = Path('reviews.json')
    output_path = Path('reviews_filtered.json')

    with open(input_path, encoding='utf-8') as f:
        data = json.load(f)

    output = {}
    for game, content in data.items():
        filtered = run_filter(content['reviews'])
        output[game] = {
            'meta': {**content['meta'], 'filtered_count': len(filtered)},
            'reviews': filtered,
        }

    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
