#!/usr/bin/env python3
"""Thirty-minute signal check, cached AI analysis and hourly safety refresh."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("SUDDENLYWHY_DATA_DIR", ROOT / "data"))
STATE_PATH = DATA_DIR / "cloud_scheduler_state.json"
RAW_PATH = DATA_DIR / "suddenlywhy_raw_top40.json"
FINAL_PATH = DATA_DIR / "suddenlywhy_final.json"
STATUS_PATH = DATA_DIR / "suddenlywhy_status.json"
HISTORY_PATHS = {
    "24h": DATA_DIR / "suddenlywhy_history_24h.json",
    "7d": DATA_DIR / "suddenlywhy_history_7d.json",
    "30d": DATA_DIR / "suddenlywhy_history_30d.json",
}
FORCE_SECONDS = int(os.environ.get("SUDDENLYWHY_FORCE_ANALYSIS_SECONDS", "3600"))
MIN_CHECK_SECONDS = 15 * 60


def outbox_path() -> Path:
    return DATA_DIR / "pending_publication.json"


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def topics_from_raw() -> list[str]:
    payload = read_json(RAW_PATH, {})
    topics = []
    for item in (payload.get("rankings") or [])[:10]:
        topic = str(item.get("topic") or "").strip().lower()
        if topic:
            topics.append(topic)
    return topics


def needs_analysis(previous: dict, current_topics: list[str], now_epoch: int) -> tuple[bool, str]:
    last_full = int(previous.get("lastFullEpoch") or 0)
    previous_topics = [str(value) for value in previous.get("topics") or []]
    if not FINAL_PATH.exists() or not previous_topics:
        return True, "first full analysis"
    if now_epoch - last_full >= FORCE_SECONDS:
        return True, "hourly safety refresh"
    if current_topics[:3] != previous_topics[:3]:
        return True, "top-three signal changed"
    changed = len(set(current_topics).symmetric_difference(previous_topics))
    if changed >= 4:
        return True, "two or more top-ten topics changed"
    return False, "signals are stable"


def run_collector(*arguments: str) -> int:
    command = [sys.executable, str(ROOT / "suddenlywhy_v85.py"), *arguments]
    return subprocess.run(command, cwd=ROOT, check=False).returncode


def queue_publication(checkpoint: dict) -> None:
    snapshots = {"now": read_json(FINAL_PATH, {"rankings": []})}
    snapshots.update({mode: read_json(path, {"rankings": []}) for mode, path in HISTORY_PATHS.items()})
    write_json(outbox_path(), {"payload": {"snapshots": snapshots, "status": read_json(STATUS_PATH, {"state": "error"})}, "checkpoint": checkpoint})


def publish() -> None:
    base_url = os.environ.get("SITE_UPLOAD_URL", "").rstrip("/")
    token = os.environ.get("SITE_UPLOAD_TOKEN", "")
    if not base_url or not token:
        raise RuntimeError("SITE_UPLOAD_URL and SITE_UPLOAD_TOKEN are required")
    pending = read_json(outbox_path(), None)
    if not isinstance(pending, dict) or not isinstance(pending.get("payload"), dict):
        raise RuntimeError("pending publication is missing or invalid")
    body = json.dumps(pending["payload"], ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}/api/admin/import",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; nowwhy-collector/1.0; +https://nowwhy.kr)",
        },
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                if response.status != 200 or json.loads(response.read()).get("ok") is not True:
                    raise RuntimeError("site did not acknowledge the publication")
            break
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f"site upload failed: HTTP {error.code}; result saved for retry") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 2:
                raise RuntimeError("site upload connection failed; result saved for retry") from None
        time.sleep(2 ** attempt)
    checkpoint = dict(pending.get("checkpoint") or {})
    if pending["payload"].get("status", {}).get("state") in {"ok", "signals_ok"}:
        checkpoint["lastPublishedEpoch"] = int(datetime.now(timezone.utc).timestamp())
    write_json(STATE_PATH, checkpoint)
    outbox_path().unlink(missing_ok=True)


def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    os.environ["SUDDENLYWHY_DATA_DIR"] = str(DATA_DIR)
    if outbox_path().exists():
        print("[scheduler] retrying saved publication without new AI analysis")
        publish()
        return 0
    previous = read_json(STATE_PATH, {})
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    elapsed = now_epoch - int(previous.get("lastPublishedEpoch") or 0)
    if 0 <= elapsed < MIN_CHECK_SECONDS:
        print("[scheduler] recent successful publication; duplicate trigger skipped")
        return 0
    previous_status = read_json(STATUS_PATH, {})
    signal_code = run_collector("--signals-only")
    if signal_code:
        write_json(STATUS_PATH, {**previous_status, "state": "error", "message": "검색 신호 확인에 실패했습니다. 마지막 정상 순위를 유지합니다.", "lastAttemptAt": datetime.now(timezone.utc).isoformat()})
        queue_publication(previous)
        publish()
        return signal_code

    checked_at = datetime.now(timezone.utc).isoformat()
    now_epoch = int(datetime.now(timezone.utc).timestamp())
    topics = topics_from_raw()
    analyze, reason = needs_analysis(previous, topics, now_epoch)
    print(f"[scheduler] {reason}; full_analysis={str(analyze).lower()}")
    result_code = 0
    if analyze:
        result_code = run_collector()
        if result_code == 0:
            topics = topics_from_raw()
    status = read_json(STATUS_PATH, {})
    status["lastCheckedAt"] = checked_at
    status["checkIntervalMinutes"] = 30
    status["analysisIntervalMinutes"] = FORCE_SECONDS // 60
    if result_code and status.get("state") not in {"error", "quota_exhausted"}:
        status.update(state="error", message="전체 분석에 실패했습니다. 마지막 정상 순위를 유지합니다.")
    write_json(STATUS_PATH, status)
    checkpoint = dict(previous)
    if analyze and result_code == 0:
        checkpoint.update(lastFullEpoch=now_epoch, topics=topics, reason=reason)
    queue_publication(checkpoint)
    publish()
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())
