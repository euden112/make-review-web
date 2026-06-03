#!/usr/bin/env python
"""기존 게임 reduce payload에 Steam 공식 종합 등급(query_summary)을 백필한다.

흐름(게임별):
  1) 클라우드 게임 상세에서 cover_image/store_url → Steam appid 추출
  2) Steam appreviews query_summary(전역: language=all&purchase_type=all) 페치
  3) 최신 keep payload의 reduce_payload.score_anchors에 공식 필드 주입
     - steam_review_score_desc / steam_total_positive / steam_total_reviews
     - steam_recommend_ratio = 공식 total_positive/total_reviews*100 (baseline 공식화)

패치 후 `.\\run_local_map.ps1 -Games "<spec>" -Replay`로 reduce만 재실행하면
클라우드가 공식 baseline + 종합 등급으로 요약을 재생성한다(재맵/재크롤 불필요).

전제: 클라우드 백엔드가 신 코드(reduce_api/ai_service/summaries) + 마이그레이션15로
재빌드돼 있어야 replay 결과가 steam_rating_* 컬럼에 영속화된다.

사용:
  python backfill_steam_rating.py --games 1-85
  python backfill_steam_rating.py --games 37,38,39 --dry-run
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
PAYLOAD_DIR = ROOT / "ai-pipeline" / "artifacts" / "reduce_payloads" / "keep"
STEAM_APPREVIEWS = "https://store.steampowered.com/appreviews/{appid}"
APPID_RE = re.compile(r"/app(?:s)?/(\d+)")


def load_env() -> dict[str, str]:
    env = {}
    envp = ROOT / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([^#=]+)=(.*)$", line)
            if m:
                env[m.group(1).strip()] = m.group(2).strip()
    return env


def resolve_appid(cloud: str, game_id: int) -> str | None:
    """게임 상세 cover/hero/store_url에서 Steam appid 추출."""
    try:
        g = requests.get(f"{cloud}/api/v1/games/{game_id}", timeout=20).json()
    except Exception as e:  # noqa: BLE001
        print(f"  game {game_id}: 상세 조회 실패 {e}")
        return None
    for key in ("cover_image", "hero_image"):
        m = APPID_RE.search(str(g.get(key) or ""))
        if m:
            return m.group(1)
    # 폴백: buy-signal store_url
    try:
        b = requests.get(f"{cloud}/api/v1/games/{game_id}/buy-signal", timeout=20).json()
        m = APPID_RE.search(str(b.get("store_url") or ""))
        if m:
            return m.group(1)
    except Exception:  # noqa: BLE001
        pass
    return None


def fetch_query_summary(appid: str) -> dict | None:
    """Steam 전역 query_summary(리뷰 본문 0건, 집계만). l=english로 desc 안정화."""
    params = {
        "json": 1,
        "language": "all",
        "purchase_type": "all",
        "num_per_page": 0,
        "filter": "all",
        "l": "english",
    }
    for attempt in range(4):
        try:
            r = requests.get(STEAM_APPREVIEWS.format(appid=appid), params=params, timeout=20)
            data = r.json()
            if data.get("success") == 1 and data.get("query_summary"):
                return data["query_summary"]
            return None
        except Exception as e:  # noqa: BLE001
            if attempt < 3:
                time.sleep(2 ** attempt)
            else:
                print(f"  appid {appid}: query_summary 실패 {e}")
    return None


def latest_payload(game_id: int) -> Path | None:
    cands = sorted(glob.glob(str(PAYLOAD_DIR / f"game_{game_id}_*.json")), key=os.path.getmtime)
    return Path(cands[-1]) if cands else None


def patch_payload(path: Path, qs: dict, dry: bool) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    rp = d.get("reduce_payload")
    if not isinstance(rp, dict):
        return "payload 구조 이상(reduce_payload 없음)"
    anchors = rp.get("score_anchors") or {}
    total = int(qs.get("total_reviews") or 0)
    pos = int(qs.get("total_positive") or 0)
    if total <= 0:
        return "공식 리뷰 0건 — 스킵"
    anchors["steam_review_score_desc"] = qs.get("review_score_desc")
    anchors["steam_total_positive"] = pos
    anchors["steam_total_reviews"] = total
    anchors["steam_recommend_ratio"] = round(pos / total * 100, 2)  # baseline 공식화
    rp["score_anchors"] = anchors
    if dry:
        return f"[dry] {qs.get('review_score_desc')} {pos}/{total} ({anchors['steam_recommend_ratio']}%)"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"patched {qs.get('review_score_desc')} {pos}/{total} ({anchors['steam_recommend_ratio']}%)"


def parse_games(spec: str) -> list[int]:
    ids: list[int] = []
    for part in spec.split(","):
        p = part.strip()
        if "-" in p:
            a, b = p.split("-", 1)
            ids += list(range(int(a), int(b) + 1))
        elif p.isdigit():
            ids.append(int(p))
    return ids


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--games", required=True, help='"1-85" | "37,38" | "1-10,20"')
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--delay", type=float, default=1.0, help="Steam 호출 간 대기(초)")
    args = ap.parse_args()

    env = load_env()
    cloud = (env.get("CLOUD_URL") or os.getenv("CLOUD_URL") or "").rstrip("/")
    if not cloud:
        print("CLOUD_URL 미설정 (.env)")
        return 1

    ids = parse_games(args.games)
    print(f"대상 {len(ids)}개: {ids}")
    ok = skip = fail = 0
    for gid in ids:
        appid = resolve_appid(cloud, gid)
        if not appid:
            print(f"game {gid}: appid 못 찾음 — 스킵")
            skip += 1
            continue
        qs = fetch_query_summary(appid)
        if not qs:
            print(f"game {gid} (appid {appid}): query_summary 없음 — 스킵")
            skip += 1
            continue
        pl = latest_payload(gid)
        if not pl:
            print(f"game {gid}: payload 없음 — 스킵")
            skip += 1
            continue
        msg = patch_payload(pl, qs, args.dry_run)
        print(f"game {gid} (appid {appid}): {msg}")
        if msg.startswith(("patched", "[dry]")):
            ok += 1
        else:
            fail += 1
        time.sleep(args.delay)
    print(f"\n완료: {ok} ok / {skip} skip / {fail} fail")
    print("다음: .\\run_local_map.ps1 -Games \"%s\" -Replay" % args.games)
    return 0


if __name__ == "__main__":
    sys.exit(main())
