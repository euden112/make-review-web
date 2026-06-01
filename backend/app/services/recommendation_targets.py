import re
from typing import Any


_OPEN_WORLD_LABEL_RE = re.compile(r"(오픈\s*월드|open\s*world|샌드박스|sandbox)", re.I)
_OPEN_WORLD_EVIDENCE_RE = re.compile(
    r"(오픈\s*월드|open\s*world|샌드박스|sandbox|탐험|explor|월드|world)",
    re.I,
)
_BUILD_VARIETY_RE = re.compile(
    r"(빌드|트리|스킬|무기|조합|선택지|선택|리롤|은혜|boon|upgrade|build|weapon|rogue|로그)",
    re.I,
)
_ACTION_RE = re.compile(r"(전투|액션|핵\s*앤\s*슬래시|핵슬|combat|action|hack)", re.I)
_STORY_RE = re.compile(r"(스토리|서사|캐릭터|신화|세계관|story|character|myth)", re.I)


def _repair_player_label(label: str, reason: str) -> str:
    """Avoid leaking unsupported broad genre labels into recommendation targets."""
    normalized_label = " ".join(str(label or "").split()).strip()
    evidence_text = " ".join(str(reason or "").split()).strip()
    if not normalized_label:
        return ""

    has_open_world_label = bool(_OPEN_WORLD_LABEL_RE.search(normalized_label))
    has_open_world_evidence = bool(_OPEN_WORLD_EVIDENCE_RE.search(evidence_text))
    if not has_open_world_label or has_open_world_evidence:
        return normalized_label

    if _BUILD_VARIETY_RE.search(evidence_text):
        return "빌드 조합과 선택지 다양성을 즐기는 플레이어"
    if _ACTION_RE.search(evidence_text):
        return "빠른 액션 전투를 즐기는 플레이어"
    if _STORY_RE.search(evidence_text):
        return "서사와 캐릭터 해석을 즐기는 플레이어"
    return "반복 플레이와 성장 변화를 즐기는 플레이어"


def sanitize_player_targets(items: Any, *, limit: int = 5) -> list[dict[str, str]]:
    if not isinstance(items, list):
        return []

    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        reason = " ".join(str(item.get("reason") or item.get("summary") or "").split()).strip()
        label = _repair_player_label(str(item.get("label") or ""), reason)
        if not label:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append({"label": label, "reason": reason})
        if len(out) >= limit:
            break
    return out
