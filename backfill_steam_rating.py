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

from steam_rating import fetch_query_summary, official_anchor_fields, resolve_appid

ROOT = Path(__file__).resolve().parent
PAYLOAD_DIR = ROOT / "ai-pipeline" / "artifacts" / "reduce_payloads" / "keep"


def load_env() -> dict[str, str]:
    env = {}
    envp = ROOT / ".env"
    if envp.exists():
        for line in envp.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([^#=]+)=(.*)$", line)
            if m:
                env[m.group(1).strip()] = m.group(2).strip()
    return env


def latest_payload(game_id: int) -> Path | None:
    cands = sorted(glob.glob(str(PAYLOAD_DIR / f"game_{game_id}_*.json")), key=os.path.getmtime)
    return Path(cands[-1]) if cands else None


def patch_payload(path: Path, qs: dict, dry: bool) -> str:
    d = json.loads(path.read_text(encoding="utf-8"))
    rp = d.get("reduce_payload")
    if not isinstance(rp, dict):
        return "payload 구조 이상(reduce_payload 없음)"
    anchors = rp.get("score_anchors") or {}
    fields = official_anchor_fields(qs)
    if not fields:
        return "공식 리뷰 0건 — 스킵"
    anchors.update(fields)
    rp["score_anchors"] = anchors
    desc = fields["steam_review_score_desc"]
    summary = f"{desc} {fields['steam_total_positive']}/{fields['steam_total_reviews']} ({fields['steam_recommend_ratio']}%)"
    if dry:
        return f"[dry] {summary}"
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    return f"patched {summary}"


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
