#!/usr/bin/env python
"""코어 평가셋(20개) 층화 선정 — 실험 1·2·3 공용.

선정 기준(stratification)
  ① faithfulness 구간: 기존 eval_ragas_reduce_result.csv 점수로 저/중/고 3분할
       저 f<0.75 · 중 0.75<=f<0.95 · 고 f>=0.95
  ② 장르: Steam 태그로 1차 장르 분류(아래 GENRE_RULES, 위에서부터 first-match)
  ③ 리뷰량: 게임별 external_reviews 수의 3분위(소/중/대)

목표 배분(20개): 저 5 · 중 8 · 고 7.
  - 저 구간은 표본이 5개뿐(0.625~0.714)이라 전부 포함(정성검토 대상과 동일).
  - 중/고는 시드 고정 그리디로 '장르 다양성 + 리뷰량 소/대 동시 포함'을 최대화.
무작위 시드: SEED=20260607 (재현용 고정). 동률 tie-break에만 사용.

입력
  - ai-pipeline/eval_ragas_reduce_result.csv  (game_id, faithfulness)
  - /tmp/games_meta.tsv  (id, title, reviews, tags-json) — 없으면 docker로 재생성
출력
  - ai-pipeline/core_eval_set.csv  (선정 20개 + 사유)
  - 콘솔: 선정 기준 헤더 + 분포표
"""
from __future__ import annotations

import csv
import json
import os
import random
import subprocess
import sys

SEED = 20260607
TARGET_TOTAL = 20
TIER_QUOTA = {"저": 5, "중": 8, "고": 7}

_THIS = os.path.dirname(os.path.abspath(__file__))
_AIPIPE = os.path.dirname(_THIS)  # ai-pipeline/
FAITH_CSV = os.path.join(_AIPIPE, "eval_ragas_reduce_result.csv")
META_TSV = "/tmp/games_meta.tsv"
OUT_CSV = os.path.join(_THIS, "core_eval_set.csv")

# 장르 1차 분류: (라벨, 매칭 태그들). 위에서부터 first-match. 구분력 높은 장르를 위로.
GENRE_RULES = [
    ("roguelike",  ["로그라이크", "로그라이트", "덱빌딩"]),
    ("fighting",   ["격투"]),
    ("racing",     ["레이싱", "드라이빙"]),
    ("sports",     ["스포츠"]),
    ("horror",     ["공포", "생존 공포"]),
    ("soulslike",  ["소울라이크"]),
    ("strategy",   ["4X", "턴제 전략", "전략", "문명", "도시 건설"]),
    ("platformer", ["플랫포머", "정밀 플랫포머"]),
    ("open_world", ["오픈 월드"]),
    ("shooter",    ["1인칭 슈터", "1인칭 슈팅", "FPS", "배틀로얄", "히어로 슈터", "슈팅", "3인칭 슈팅"]),
    ("narrative",  ["풍부한 스토리", "선택지", "비주얼 노벨", "인터랙티브 드라마", "내러티브", "걷기 시뮬레이션"]),
    ("rpg",        ["액션 RPG", "JRPG", "RPG"]),
    ("coop_mp",    ["협동", "온라인 협동", "MOBA", "멀티플레이어"]),
]


def _genre(tags: list[str]) -> str:
    for label, keys in GENRE_RULES:
        if any(k in tags for k in keys):
            return label
    return "action"


def _faith_tier(f: float) -> str:
    if f < 0.75:
        return "저"
    if f < 0.95:
        return "중"
    return "고"


def _ensure_meta() -> None:
    if os.path.exists(META_TSV):
        return
    sql = (
        "SELECT g.id, g.canonical_title, COALESCE(rc.cnt,0), "
        "COALESCE((SELECT pm.platform_meta_json->>'tags' FROM game_platform_map pm "
        "WHERE pm.game_id=g.id AND pm.platform_meta_json->'tags' IS NOT NULL LIMIT 1),'[]') "
        "FROM games g LEFT JOIN (SELECT game_id,COUNT(*) cnt FROM external_reviews "
        "WHERE is_deleted=false GROUP BY game_id) rc ON rc.game_id=g.id ORDER BY g.id;"
    )
    out = subprocess.check_output([
        "docker", "exec", "capstone_postgres", "psql", "-U", "postgres", "-d",
        "review_db", "-t", "-A", "-F", "\t", "-c", sql,
    ], text=True)
    with open(META_TSV, "w", encoding="utf-8") as fh:
        fh.write(out)


def _load() -> list[dict]:
    faith: dict[int, float] = {}
    with open(FAITH_CSV, encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            try:
                faith[int(r["game_id"])] = float(r["faithfulness"])
            except (TypeError, ValueError):
                pass

    games: list[dict] = []
    with open(META_TSV, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4:
                continue
            gid = int(parts[0])
            if gid not in faith:
                continue
            tags = json.loads(parts[3] or "[]")
            games.append({
                "id": gid,
                "title": parts[1],
                "reviews": int(parts[2]),
                "genre": _genre(tags),
                "faith": faith[gid],
                "tier": _faith_tier(faith[gid]),
            })
    return games


def _vol_tiers(games: list[dict]) -> None:
    counts = sorted(g["reviews"] for g in games)
    n = len(counts)
    lo, hi = counts[n // 3], counts[2 * n // 3]
    for g in games:
        g["vol"] = "소" if g["reviews"] <= lo else ("대" if g["reviews"] >= hi else "중")


def _pick_tier(pool: list[dict], quota: int, rng: random.Random,
               used_genres: set[str]) -> list[dict]:
    """장르 다양성 + 리뷰량 소/대 동시 포함을 우선하는 시드 고정 그리디."""
    chosen: list[dict] = []
    remaining = list(pool)
    rng.shuffle(remaining)  # 동률 tie-break만 무작위(시드 고정)
    # 1차: 아직 안 쓴 장르 우선
    for g in list(remaining):
        if len(chosen) >= quota:
            break
        if g["genre"] not in used_genres:
            chosen.append(g)
            used_genres.add(g["genre"])
            remaining.remove(g)
    # 2차: 리뷰량 소/대 균형 보강
    have_vols = {g["vol"] for g in chosen}
    for want in ("소", "대"):
        if len(chosen) >= quota or want in have_vols:
            continue
        for g in list(remaining):
            if g["vol"] == want:
                chosen.append(g)
                remaining.remove(g)
                have_vols.add(want)
                break
    # 3차: 남은 자리 채우기
    for g in remaining:
        if len(chosen) >= quota:
            break
        chosen.append(g)
    return chosen


def main() -> int:
    _ensure_meta()
    games = _load()
    _vol_tiers(games)
    rng = random.Random(SEED)

    by_tier: dict[str, list[dict]] = {"저": [], "중": [], "고": []}
    for g in games:
        by_tier[g["tier"]].append(g)

    used_genres: set[str] = set()
    selected: list[dict] = []
    for tier in ("저", "중", "고"):
        quota = min(TIER_QUOTA[tier], len(by_tier[tier]))
        picked = _pick_tier(by_tier[tier], quota, rng, used_genres)
        selected.extend(picked)

    selected.sort(key=lambda g: (g["tier"] != "저", g["tier"] != "중", -g["faith"], g["id"]))

    # ---- 출력 ----
    print("=" * 72)
    print("코어 평가셋 (20개) — 실험 1·2·3 공용")
    print(f"선정 기준: ① faithfulness 저/중/고  ② 장르(Steam 태그)  ③ 리뷰량 소/중/대")
    print(f"배분 목표: 저 {TIER_QUOTA['저']} · 중 {TIER_QUOTA['중']} · 고 {TIER_QUOTA['고']}   |   시드 {SEED}")
    print("=" * 72)
    hdr = f"{'id':>3}  {'title':<34} {'faith':>5} {'tier':>4} {'genre':<11} {'reviews':>7} {'vol':>3}"
    print(hdr)
    print("-" * len(hdr))
    for g in selected:
        print(f"{g['id']:>3}  {g['title'][:34]:<34} {g['faith']:>5.3f} {g['tier']:>4} "
              f"{g['genre']:<11} {g['reviews']:>7} {g['vol']:>3}")

    def _dist(key):
        d: dict[str, int] = {}
        for g in selected:
            d[g[key]] = d.get(g[key], 0) + 1
        return ", ".join(f"{k}:{v}" for k, v in sorted(d.items()))

    print("-" * len(hdr))
    print(f"분포  tier  → {_dist('tier')}")
    print(f"분포  genre → {_dist('genre')}")
    print(f"분포  vol   → {_dist('vol')}")

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["game_id", "title", "faithfulness", "faith_tier",
                    "genre", "reviews", "vol_tier"])
        for g in selected:
            w.writerow([g["id"], g["title"], f"{g['faith']:.3f}", g["tier"],
                        g["genre"], g["reviews"], g["vol"]])
    print(f"\n→ {OUT_CSV} ({len(selected)}개)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
