#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import csv
import hashlib
import html
import json
import math
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Iterable

VERSION = "V8.5"
GOOGLE_RSS_URL = "https://trends.google.com/trending/rss?geo=KR"
NAVER_TREND_URL = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"
NAVER_NEWS_URL = "https://naverapihub.apigw.ntruss.com/search/v1/news"
OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"

POOL_HOURS = 24
POOL_HOUR_WINDOWS = (24, 48, 72)
EVIDENCE_HOUR_WINDOWS = (24, 48, 72)
NAVER_LOOKBACK_DAYS = 20
NAVER_WINDOW_POINTS = 14
RECENT_POINTS = 2
TOP_COUNT = 10
RAW_CANDIDATE_COUNT = 40
MIN_PUBLISHABLE_TO_REPLACE = max(1, min(TOP_COUNT, int(os.getenv("SUDDENLYWHY_MIN_PUBLISHABLE_TO_REPLACE", "5"))))
NEWS_PER_TOPIC = 10
BROAD_NEWS_PER_QUERY = 10
BROAD_MAX_ARTICLES = 30

FAST_MODEL = os.getenv("SUDDENLYWHY_FAST_MODEL", "gpt-5.6-luna")
VERIFY_MODEL = os.getenv("SUDDENLYWHY_VERIFY_MODEL", "gpt-5.6-terra")
MODEL = FAST_MODEL
EMBEDDING_MODEL = os.getenv("SUDDENLYWHY_EMBEDDING_MODEL", "text-embedding-3-small")
USE_EMBEDDINGS = os.getenv("SUDDENLYWHY_USE_EMBEDDINGS", "1").strip().lower() not in {"0", "false", "no", "off"}
LEXICAL_THRESHOLD = 0.08
SEMANTIC_THRESHOLD = float(os.getenv("SUDDENLYWHY_SEMANTIC_THRESHOLD", "0.78"))

API_USAGE: dict[str, dict[str, int]] = defaultdict(lambda:{"requests":0,"inputTokens":0,"outputTokens":0})
MODEL_USD_PER_MILLION = {
    # 2026-09-01 OpenAI 공식 모델 비교표 기준. 모델 가격 변경 시 함께 갱신한다.
    "gpt-5.6-luna": (0.20, 1.20),
    "gpt-5.6-terra": (2.00, 12.00),
}

ALIASES_TO_CANONICAL = {
    "epl": "프리미어리그",
    "premier league": "프리미어리그",
    "english premier league": "프리미어리그",
    "아스날": "아스널",
    "arsenal": "아스널",
    "fc barcelona": "바르셀로나",
    "barcelona": "바르셀로나",
}
CANONICAL_ALIASES = {
    "프리미어리그": ["프리미어리그", "EPL", "Premier League", "English Premier League"],
    "아스널": ["아스널", "아스날", "Arsenal"],
    "바르셀로나": ["바르셀로나", "Barcelona", "FC Barcelona"],
}


def now_local() -> datetime:
    return datetime.now().astimezone()


def desktop_dir() -> Path:
    home = Path.home()
    if (home / "Desktop").exists():
        return home / "Desktop"
    if (home / "OneDrive" / "Desktop").exists():
        return home / "OneDrive" / "Desktop"
    return home


DATA_DIR = Path(os.getenv("SUDDENLYWHY_DATA_DIR") or (desktop_dir() / "suddenlywhy-data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "suddenlywhy.db"
TOP_PATH = DATA_DIR / "suddenlywhy_top10.json"
RAW_PATH = DATA_DIR / "suddenlywhy_raw_top40.json"
NEWS_PATH = DATA_DIR / "suddenlywhy_news_evidence.json"
CANDIDATE_PATH = DATA_DIR / "suddenlywhy_why_candidates.json"
BROAD_PATH = DATA_DIR / "suddenlywhy_broad_rescue.json"
FINAL_PATH = DATA_DIR / "suddenlywhy_final.json"
LAST_GOOD_PATH = DATA_DIR / "suddenlywhy_last_good.json"
STATUS_PATH = DATA_DIR / "suddenlywhy_status.json"
HISTORY_24H_PATH = DATA_DIR / "suddenlywhy_history_24h.json"
HISTORY_7D_PATH = DATA_DIR / "suddenlywhy_history_7d.json"
HISTORY_30D_PATH = DATA_DIR / "suddenlywhy_history_30d.json"
LEGACY_GOOGLE_HISTORY = DATA_DIR / "google_history.csv"


class CreditBalanceExhausted(RuntimeError):
    """OpenAI API 크레딧 소진을 일반 후보 실패와 구분한다."""


def is_credit_error_text(text: str) -> bool:
    value = str(text or "").lower()
    return any(marker in value for marker in (
        "credit_balance_exhausted",
        "insufficient_quota",
        "you have no credits remaining",
        "add credits to continue using the api",
    ))


def record_response_usage(model: str, resp: dict[str, Any]) -> None:
    usage=resp.get("usage") or {}
    row=API_USAGE[model]
    row["requests"]+=1
    row["inputTokens"]+=int(usage.get("input_tokens") or 0)
    row["outputTokens"]+=int(usage.get("output_tokens") or 0)


def usage_summary() -> dict[str, Any]:
    models={name:dict(values) for name,values in API_USAGE.items()}
    estimate=0.0; priced=True
    for name,values in models.items():
        rates=MODEL_USD_PER_MILLION.get(name)
        if not rates:
            priced=False
            continue
        estimate+=(values["inputTokens"]*rates[0]+values["outputTokens"]*rates[1])/1_000_000
    return {
        "models":models,
        "responseRequests":sum(v["requests"] for v in models.values()),
        "estimatedResponseCostUsd":round(estimate,6) if priced else None,
        "note":"텍스트 Responses 호출 추정치이며 임베딩·세금·가격 변경은 포함하지 않습니다.",
    }


def log(msg: str = "") -> None:
    print(msg, flush=True)


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def normalize_text(text: str) -> str:
    x = (text or "").strip().lower()
    x = re.sub(r"[－–—]", "-", x)
    return re.sub(r"\s+", " ", x).strip()


def normalize_compact(text: str) -> str:
    return re.sub(r"[^가-힣a-z0-9]", "", (text or "").lower())


def canonical_topic(title: str) -> str:
    return ALIASES_TO_CANONICAL.get(normalize_text(title), (title or "").strip())


def topic_dedupe_key(topic: str) -> str:
    key = re.sub(r'''[\s\-_·.,'"“”‘’(){}\[\]]+''', "", (topic or "").lower())
    if key in {"크보", "kbo"}:
        return "kbo"
    if key in {"엘지트윈스", "lg트윈스"}:
        return "lg트윈스"
    return key


def convert_traffic(value: Any) -> float:
    s = str(value or "").strip().upper().replace(",", "").replace("+", "").replace(" ", "")
    if not s:
        return 0.0
    mult = 1.0
    if s.endswith("K"):
        mult, s = 1000.0, s[:-1]
    elif s.endswith("M"):
        mult, s = 1_000_000.0, s[:-1]
    elif s.endswith("B"):
        mult, s = 1_000_000_000.0, s[:-1]
    try:
        return float(s) * mult
    except ValueError:
        return 0.0


def clean_html(text: str) -> str:
    x = re.sub(r"<[^>]+>", "", text or "")
    return re.sub(r"\s+", " ", html.unescape(x)).strip()


def clean_headline(text: str) -> str:
    return re.sub(r"^\[[^\]]+\]\s*", "", clean_html(text)).strip()


def tokens(text: str) -> list[str]:
    out, seen = [], set()
    for t in re.findall(r"[가-힣A-Za-z0-9]+", (text or "").lower()):
        if len(t) >= 2 and t not in seen:
            out.append(t)
            seen.add(t)
    return out


def ngrams(text: str, n: int = 3) -> set[str]:
    x = normalize_compact(text)
    if not x:
        return set()
    if len(x) < n:
        return {x}
    return {x[i:i+n] for i in range(len(x) - n + 1)}


def jaccard(a: str, b: str) -> float:
    ga, gb = ngrams(a), ngrams(b)
    union = ga | gb
    return len(ga & gb) / len(union) if union else 0.0


def is_noise_topic(topic: str) -> bool:
    if not (topic or "").strip():
        return True
    t = topic.lower()
    patterns = ["뉴스, 커뮤니티", "뉴스 커뮤니티", "빠르고 정확한", "공식 홈페이지", "official site", "download app"]
    if any(p in t for p in patterns):
        return True
    return len(t) >= 34 and (" - " in t or " | " in t)


GENERIC_FAST_REJECT = {
    "스포츠", "탑승", "뉴스", "연예", "사회", "문화", "정치", "경제",
    "사건", "사고", "정보", "검색", "화제", "이슈"
}

def is_generic_fast_reject(topic: str) -> bool:
    """정확한 사건/대상을 가리키지 않는 극단적으로 넓은 단일어만 빠르게 제외한다.
    '야구 순위'처럼 구조 가능한 복합 검색어는 제외하지 않는다.
    """
    return normalize_text(topic) in GENERIC_FAST_REJECT


def parse_article_date(text: str) -> datetime | None:
    if not text:
        return None
    try:
        dt = parsedate_to_datetime(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone()
    except Exception:
        try:
            dt = datetime.fromisoformat(text)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now_local().tzinfo)
            return dt.astimezone()
        except Exception:
            return None


def source_host(original_link: str, link: str) -> str:
    candidate = html.unescape((original_link or link or "").strip())
    try:
        host = urllib.parse.urlparse(candidate).hostname or ""
        return re.sub(r"^www\.", "", host.lower())
    except Exception:
        return ""


def unique(values: Iterable[str], limit: int | None = None) -> list[str]:
    out, seen = [], set()
    for value in values:
        v = str(value or "").strip()
        k = v.lower()
        if not v or k in seen:
            continue
        out.append(v)
        seen.add(k)
        if limit and len(out) >= limit:
            break
    return out


def json_dump(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def json_load(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def ranking_count(payload: Any) -> int:
    if not isinstance(payload, dict) or not isinstance(payload.get("rankings"), list):
        return 0
    return len(payload["rankings"])


def usable_final(payload: Any, min_count: int = 1) -> bool:
    return ranking_count(payload) >= max(1, min_count)


def last_successful_at() -> str:
    for path in (FINAL_PATH, LAST_GOOD_PATH):
        payload = json_load(path, {})
        if usable_final(payload):
            return str(payload.get("generatedAt") or "")
    return ""


def preserve_last_good() -> None:
    payload = json_load(FINAL_PATH, {})
    if usable_final(payload, MIN_PUBLISHABLE_TO_REPLACE):
        json_dump(LAST_GOOD_PATH, payload)


def restore_last_good_if_needed(min_count: int = 1, force: bool = False) -> bool:
    min_count=max(1, min_count)
    if not force and usable_final(json_load(FINAL_PATH, {}), min_count):
        return False
    payload = json_load(LAST_GOOD_PATH, {})
    if not usable_final(payload, min_count) and DB_PATH.exists():
        conn=None
        try:
            conn=sqlite3.connect(DB_PATH)
            conn.row_factory=sqlite3.Row
            snapshot=conn.execute(
                "SELECT generated_at FROM ranking_history WHERE publishable=1 AND item_json IS NOT NULL "
                "GROUP BY generated_at HAVING COUNT(*)>=? ORDER BY datetime(generated_at) DESC LIMIT 1",
                (min_count,),
            ).fetchone()
            if snapshot:
                rows=conn.execute(
                    "SELECT item_json FROM ranking_history WHERE generated_at=? AND publishable=1 ORDER BY rank",
                    (snapshot["generated_at"],),
                ).fetchall()
                rankings=[]
                for row in rows:
                    try: rankings.append(json.loads(row["item_json"]))
                    except Exception: continue
                if len(rankings) >= min_count:
                    payload={
                        "generatedAt":str(snapshot["generated_at"]),
                        "version":VERSION,
                        "recoveredFrom":"SQLite ranking_history",
                        "rankings":rankings,
                    }
        except Exception:
            payload={}
        finally:
            if conn is not None:
                conn.close()
    if not usable_final(payload, min_count):
        return False
    json_dump(FINAL_PATH, payload)
    json_dump(TOP_PATH, payload)
    json_dump(LAST_GOOD_PATH, payload)
    return True


def write_status(state: str, message: str, run_now: datetime,
                 last_ok: str | None = None, **extra: Any) -> None:
    payload = {
        "state": state,
        "message": message,
        "version": VERSION,
        "lastAttemptAt": run_now.strftime("%Y-%m-%d %H:%M:%S"),
        "lastSuccessfulAt": last_ok if last_ok is not None else last_successful_at(),
        **extra,
    }
    json_dump(STATUS_PATH, payload)


def windows_alert(title: str, message: str) -> None:
    if os.name != "nt":
        return
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND)
    except Exception:
        pass
    try:
        safe_message=message.replace("'", "''")
        safe_title=title.replace("'", "''")
        script = (
            "Add-Type -AssemblyName PresentationFramework; "
            f"[System.Windows.MessageBox]::Show('{safe_message}', '{safe_title}', 'OK', 'Error') | Out-Null"
        )
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.Popen(
            ["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
            creationflags=flags,
        )
    except Exception:
        pass


def handle_quota_failure(run_now: datetime) -> None:
    restored = restore_last_good_if_needed()
    write_status(
        "quota_exhausted",
        "OpenAI API 크레딧이 부족해 새 분석을 중단했습니다.",
        run_now,
        finalPreserved=usable_final(json_load(FINAL_PATH, {})),
        restoredFromBackup=restored,
        apiUsage=usage_summary(),
        billingUrl="https://platform.openai.com/settings/organization/billing/overview",
    )
    border = "!" * 72
    log(f"\n{border}")
    log("!!  OPENAI API 크레딧 소진 - 이번 분석을 즉시 중단했습니다.")
    log("!!  마지막 정상 TOP10은 그대로 보존되어 홈페이지에 계속 표시됩니다.")
    log("!!  충전: https://platform.openai.com/settings/organization/billing/overview")
    log(border)
    windows_alert("갑자기왜? API 크레딧 소진", "새 분석을 중단하고 마지막 정상 TOP10을 보존했습니다. OpenAI API 크레딧을 확인해주세요.")


def http_json(url: str, method: str = "GET", headers: dict[str, str] | None = None,
              payload: Any | None = None, timeout: int = 50) -> Any:
    h = {"User-Agent": "SuddenlyWhy/7.0", "Accept": "application/json, */*"}
    if headers:
        h.update(headers)
    body = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        h.setdefault("Content-Type", "application/json; charset=utf-8")
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        if is_credit_error_text(detail):
            raise CreditBalanceExhausted("OpenAI API 크레딧이 소진되었습니다. 충전 후 다시 실행해주세요.") from e
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail[:1500]}") from e


def http_text(url: str, timeout: int = 50) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 SuddenlyWhy/7.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {detail[:1500]}") from e


# ---------- SQLite ----------
def connect_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS google_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      collected_at TEXT NOT NULL,
      canonical_topic TEXT NOT NULL,
      title TEXT NOT NULL,
      approx_traffic REAL NOT NULL,
      approx_raw TEXT,
      pub_date TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_gh_time ON google_history(collected_at);
    CREATE INDEX IF NOT EXISTS idx_gh_topic ON google_history(canonical_topic);

    CREATE TABLE IF NOT EXISTS runs(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      generated_at TEXT NOT NULL,
      version TEXT NOT NULL,
      model TEXT
    );

    CREATE TABLE IF NOT EXISTS ranking_history(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      run_id INTEGER NOT NULL,
      generated_at TEXT NOT NULL,
      rank INTEGER NOT NULL,
      topic TEXT NOT NULL,
      dedupe_key TEXT NOT NULL,
      score REAL,
      publishable INTEGER NOT NULL,
      explanation_type TEXT,
      question TEXT,
      why_now TEXT,
      summary TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rh_time ON ranking_history(generated_at);
    CREATE INDEX IF NOT EXISTS idx_rh_topic ON ranking_history(dedupe_key);

    CREATE TABLE IF NOT EXISTS cache(
      cache_key TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      value_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS embedding_cache(
      text_hash TEXT PRIMARY KEY,
      model TEXT NOT NULL,
      vector_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    ''')
    # V7.1 migration: 기존 V7 DB를 그대로 이어서 사용한다.
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ranking_history)").fetchall()}
    if "raw_rank" not in cols:
        conn.execute("ALTER TABLE ranking_history ADD COLUMN raw_rank INTEGER")
    if "item_json" not in cols:
        conn.execute("ALTER TABLE ranking_history ADD COLUMN item_json TEXT")
    conn.commit()
    return conn


def import_legacy_history(conn: sqlite3.Connection) -> int:
    if conn.execute("SELECT COUNT(*) FROM google_history").fetchone()[0] > 0:
        return 0
    if not LEGACY_GOOGLE_HISTORY.exists():
        return 0
    imported = 0
    try:
        with LEGACY_GOOGLE_HISTORY.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                topic = (row.get("canonicalTopic") or "").strip()
                collected = (row.get("collectedAt") or "").strip()
                if not topic or not collected:
                    continue
                try:
                    traffic = float(row.get("approxTraffic") or 0)
                except Exception:
                    traffic = 0.0
                conn.execute(
                    "INSERT INTO google_history(collected_at,canonical_topic,title,approx_traffic,approx_raw,pub_date) VALUES(?,?,?,?,?,?)",
                    (collected, topic, row.get("title") or topic, traffic, row.get("approxRaw"), row.get("pubDate")),
                )
                imported += 1
        conn.commit()
        return imported
    except Exception:
        conn.rollback()
        return 0


def cache_get(conn: sqlite3.Connection, key: str, max_days: int = 14) -> Any | None:
    row = conn.execute("SELECT value_json,created_at FROM cache WHERE cache_key=?", (key,)).fetchone()
    if not row:
        return None
    try:
        created = datetime.fromisoformat(row["created_at"])
        if created.tzinfo is None:
            created = created.replace(tzinfo=now_local().tzinfo)
        if now_local() - created.astimezone() > timedelta(days=max_days):
            return None
        return json.loads(row["value_json"])
    except Exception:
        return None


def cache_put(conn: sqlite3.Connection, key: str, kind: str, value: Any) -> None:
    conn.execute(
        "INSERT INTO cache(cache_key,kind,value_json,created_at) VALUES(?,?,?,?) ON CONFLICT(cache_key) DO UPDATE SET kind=excluded.kind,value_json=excluded.value_json,created_at=excluded.created_at",
        (key, kind, json.dumps(value, ensure_ascii=False), now_local().isoformat()),
    )
    conn.commit()


# ---------- Auth/API ----------
def naver_headers() -> dict[str, str]:
    cid = os.getenv("NAVER_API_HUB_CLIENT_ID", "").strip()
    sec = os.getenv("NAVER_API_HUB_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        raise RuntimeError("NAVER_API_HUB_CLIENT_ID / NAVER_API_HUB_CLIENT_SECRET 환경변수가 없습니다.")
    return {"X-NCP-APIGW-API-KEY-ID": cid, "X-NCP-APIGW-API-KEY": sec}


def openai_key() -> str:
    key = os.getenv("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 없습니다.")
    return key


# ---------- Google + NAVER trend ----------
def localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1].lower()


def collect_google(conn: sqlite3.Connection, run_now: datetime) -> list[dict[str, Any]]:
    root = ET.fromstring(http_text(GOOGLE_RSS_URL))
    items = []
    for elem in root.iter():
        if localname(elem.tag) != "item":
            continue
        vals = {}
        for child in list(elem):
            vals[localname(child.tag)] = (child.text or "").strip()
        title = vals.get("title", "")
        if not title:
            continue
        raw = vals.get("approx_traffic", vals.get("approxtraffic", ""))
        row = {
            "collectedAt": run_now.strftime("%Y-%m-%d %H:%M:%S"),
            "canonicalTopic": canonical_topic(title),
            "title": title,
            "approxTraffic": convert_traffic(raw),
            "approxRaw": raw,
            "pubDate": vals.get("pubdate", ""),
        }
        items.append(row)
    if not items:
        raise RuntimeError("Google Trends RSS에서 항목을 가져오지 못했습니다.")
    conn.executemany(
        "INSERT INTO google_history(collected_at,canonical_topic,title,approx_traffic,approx_raw,pub_date) VALUES(?,?,?,?,?,?)",
        [(x["collectedAt"],x["canonicalTopic"],x["title"],x["approxTraffic"],x["approxRaw"],x["pubDate"]) for x in items],
    )
    conn.commit()
    return items


def recent_pool(conn: sqlite3.Connection, run_now: datetime, hours: int = POOL_HOURS) -> list[dict[str, Any]]:
    cutoff = (run_now - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT collected_at,canonical_topic,title,approx_traffic,pub_date FROM google_history WHERE datetime(collected_at)>=datetime(?) ORDER BY datetime(collected_at)",
        (cutoff,),
    ).fetchall()
    grouped = defaultdict(list)
    for r in rows:
        grouped[r["canonical_topic"]].append(r)
    out = []
    for topic, group in grouped.items():
        out.append({
            "canonicalTopic": topic,
            "titles": " | ".join(unique(r["title"] for r in group)),
            "firstSeen": group[0]["collected_at"],
            "lastSeen": group[-1]["collected_at"],
            "hits": len(group),
            "latestTraffic": float(group[-1]["approx_traffic"] or 0),
            "maxTraffic": max(float(r["approx_traffic"] or 0) for r in group),
            "latestPubDate": group[-1]["pub_date"] or "",
        })
    return sorted(out, key=lambda x:x["maxTraffic"], reverse=True)


def query_naver_trend(pool: list[dict[str, Any]], run_now: datetime) -> dict[str, dict[str, Any]]:
    results = []
    for i in range(0, len(pool), 5):
        batch = pool[i:i+5]
        groups = []
        for c in batch:
            topic = c["canonicalTopic"]
            ks = []
            if c.get("titles"):
                ks += [x.strip() for x in c["titles"].split("|")]
            ks += CANONICAL_ALIASES.get(topic, [])
            groups.append({"groupName": topic, "keywords": unique(ks, 20) or [topic]})
        payload = {
            "startDate": (run_now - timedelta(days=NAVER_LOOKBACK_DAYS)).strftime("%Y-%m-%d"),
            "endDate": run_now.strftime("%Y-%m-%d"),
            "timeUnit": "date",
            "keywordGroups": groups,
        }
        try:
            resp = http_json(NAVER_TREND_URL, "POST", naver_headers(), payload)
            results += resp.get("results") or []
        except Exception as e:
            log(f"  [주의] NAVER Trend batch 실패: {e}")
        time.sleep(0.12)
    out = {}
    for r in results:
        series = sorted(r.get("data") or [], key=lambda x:x.get("period", ""))
        if len(series) < 3:
            continue
        window = series[-min(NAVER_WINDOW_POINTS, len(series)):]
        rc = min(RECENT_POINTS, len(window)-1)
        recent, baseline = window[-rc:], window[:-rc]
        if not baseline:
            continue
        recent_avg = sum(float(x.get("ratio") or 0) for x in recent) / len(recent)
        baseline_avg = sum(float(x.get("ratio") or 0) for x in baseline) / len(baseline)
        spike = recent_avg / max(baseline_avg, 0.1)
        raw = clamp(50 + 25*math.log(spike,2), 0, 100) if spike > 0 else 50
        confidence = min(1.0, len(window)/NAVER_WINDOW_POINTS)
        out[str(r.get("title", ""))] = {
            "spikeRatio": spike,
            "growthPercent": (spike-1)*100,
            "dataPoints": len(window),
            "trendScore": 50 + confidence*(raw-50),
            "latestPeriod": window[-1].get("period", ""),
        }
    return out


def score_pool(pool: list[dict[str, Any]], nmap: dict[str, dict[str, Any]], run_now: datetime) -> list[dict[str, Any]]:
    logs = [math.log10(max(1,float(c["maxTraffic"]))) for c in pool]
    mn, mx = (min(logs), max(logs)) if logs else (0,0)
    scored = []
    for c in pool:
        lt = math.log10(max(1,float(c["maxTraffic"])))
        gtraffic = 100*((lt-mn)/(mx-mn)) if mx>mn else 50
        try:
            last = datetime.fromisoformat(c["lastSeen"])
            if last.tzinfo is None:
                last = last.replace(tzinfo=run_now.tzinfo)
        except Exception:
            last = run_now
        hours = max(0,(run_now-last.astimezone(run_now.tzinfo)).total_seconds()/3600)
        grecency = clamp(100*(0.5**(hours/12)),0,100)
        n = nmap.get(c["canonicalTopic"])
        nscore = float(n["trendScore"]) if n else 50
        spike = float(n["spikeRatio"]) if n else 0
        growth = float(n["growthPercent"]) if n else 0
        points = int(n["dataPoints"]) if n else 0
        latest = str(n["latestPeriod"]) if n else ""
        final = 0.40*gtraffic + 0.35*grecency + 0.25*nscore
        scored.append({
            "topic":c["canonicalTopic"],"observedTitles":c["titles"],"maxGoogleTraffic":float(c["maxTraffic"]),
            "googleTrafficScore":round(gtraffic,1),"googleRecencyScore":round(grecency,1),
            "naverSpikeRatio":round(spike,2),"naverGrowthPercent":round(growth,1),"naverTrendScore":round(nscore,1),
            "naverDataPoints":points,"naverLatestPeriod":latest,"suddenlyWhyScore":round(final,1),
            "firstSeen":c["firstSeen"],"lastSeen":c["lastSeen"],"rssHits":int(c["hits"]),
        })
    scored.sort(key=lambda x:x["suddenlyWhyScore"], reverse=True)
    out, seen = [], set()
    for x in scored:
        k = topic_dedupe_key(x["topic"])
        if k and k not in seen:
            out.append(x); seen.add(k)
    return out


def naver_news(query: str, display: int = 5, sort: str = "date") -> list[dict[str, Any]]:
    if sort not in {"date", "sim"}:
        raise ValueError(f"지원하지 않는 NAVER 뉴스 정렬: {sort}")
    params = urllib.parse.urlencode({"query":query,"display":display,"start":1,"sort":sort,"format":"json"})
    resp = http_json(f"{NAVER_NEWS_URL}?{params}", headers=naver_headers())
    out = []
    for a in resp.get("items") or []:
        link, original = str(a.get("link") or ""), str(a.get("originallink") or "")
        out.append({
            "query":query,"searchSort":sort,"title":clean_html(str(a.get("title") or "")),"description":clean_html(str(a.get("description") or "")),
            "pubDate":str(a.get("pubDate") or ""),"link":link,"originalLink":original,"publisherHost":source_host(original,link),
        })
    return out


def blended_topic_news(query: str, display: int = 10) -> list[dict[str, Any]]:
    """관련도순으로 원인 기사를 살리고 최신순으로 새 보도를 보충한다."""
    collected=[]
    for sort in ("sim", "date"):
        collected.extend(naver_news(query,display,sort))
    return dedupe_articles(collected)


def prior_verified_articles(conn: sqlite3.Connection, topic: str, run_now: datetime,
                            max_hours: int = 72, limit: int = 12) -> list[dict[str, Any]]:
    """같은 검색어가 계속 상승 중이면 최근 실행에서 이미 교차검증한 기사를 재활용한다."""
    rows=conn.execute(
        "SELECT item_json FROM ranking_history WHERE dedupe_key=? AND publishable=1 ORDER BY datetime(generated_at) DESC LIMIT 8",
        (topic_dedupe_key(topic),)
    ).fetchall()
    out=[]
    for row in rows:
        try: item=json.loads(row["item_json"])
        except Exception: continue
        for source in item.get("sources") or []:
            dt=parse_article_date(source.get("pubDate", ""))
            if not dt: continue
            age=(run_now-dt.astimezone(run_now.tzinfo)).total_seconds()/3600
            if not (-2<=age<=max_hours): continue
            link=str(source.get("link") or ""); original=str(source.get("originalLink") or "")
            out.append({
                "query":topic,"searchSort":"verified-history","title":str(source.get("title") or ""),
                "description":"","pubDate":str(source.get("pubDate") or ""),"link":link,"originalLink":original,
                "publisherHost":str(source.get("publisherHost") or source_host(original,link)),
            })
            if len(out)>=limit: return dedupe_articles(out)
    return dedupe_articles(out)


# ---------- OpenAI ----------
def openai_output_text(resp: dict[str, Any]) -> str:
    for item in resp.get("output") or []:
        if item.get("type") == "message":
            for c in item.get("content") or []:
                if c.get("type") == "output_text":
                    return str(c.get("text") or "")
    return str(resp.get("output_text") or "")


def parse_json_object(text: str) -> dict[str, Any]:
    clean = re.sub(r"^\s*```(?:json)?\s*", "", (text or "").strip(), flags=re.I)
    clean = re.sub(r"\s*```\s*$", "", clean)
    s,e = clean.find("{"),clean.rfind("}")
    if s<0 or e<=s:
        raise RuntimeError("OpenAI 응답에서 JSON 객체를 찾지 못했습니다.")
    return json.loads(clean[s:e+1])


def openai_json(instructions: str, prompt_input: str, schema_hint: str,
                model: str = FAST_MODEL) -> dict[str, Any]:
    full = instructions + "\n\n반드시 JSON 객체 하나만 출력하세요. 마크다운 코드블록이나 설명 문장은 금지합니다.\n\n" + schema_hint
    resp = http_json(
        OPENAI_RESPONSES_URL,"POST",
        {"Authorization":f"Bearer {openai_key()}","Content-Type":"application/json"},
        {"model":model,"instructions":full,"input":prompt_input},90,
    )
    record_response_usage(model,resp)
    text = openai_output_text(resp)
    if not text.strip():
        raise RuntimeError("OpenAI 응답에서 output_text를 찾지 못했습니다.")
    return parse_json_object(text)


def embeddings(conn: sqlite3.Connection, texts: list[str]) -> list[list[float]] | None:
    if not USE_EMBEDDINGS or not texts:
        return None
    vecs: list[list[float] | None] = [None]*len(texts)
    missing_i, missing_t = [], []
    for i,t in enumerate(texts):
        h = sha256_text(EMBEDDING_MODEL+"\n"+t)
        row = conn.execute("SELECT vector_json FROM embedding_cache WHERE text_hash=? AND model=?",(h,EMBEDDING_MODEL)).fetchone()
        if row:
            try:
                vecs[i]=json.loads(row["vector_json"]); continue
            except Exception:
                pass
        missing_i.append(i); missing_t.append(t)
    if missing_t:
        try:
            resp = http_json(
                OPENAI_EMBEDDINGS_URL,"POST",
                {"Authorization":f"Bearer {openai_key()}","Content-Type":"application/json"},
                {"model":EMBEDDING_MODEL,"input":missing_t},90,
            )
            data = sorted(resp.get("data") or [], key=lambda x:int(x.get("index",0)))
            if len(data)!=len(missing_t):
                raise RuntimeError("Embedding 응답 개수 불일치")
            for slot,item in zip(missing_i,data):
                v=[float(x) for x in item.get("embedding") or []]
                vecs[slot]=v
                h=sha256_text(EMBEDDING_MODEL+"\n"+texts[slot])
                conn.execute("INSERT INTO embedding_cache(text_hash,model,vector_json,created_at) VALUES(?,?,?,?) ON CONFLICT(text_hash) DO UPDATE SET model=excluded.model,vector_json=excluded.vector_json,created_at=excluded.created_at",(h,EMBEDDING_MODEL,json.dumps(v),now_local().isoformat()))
            conn.commit()
        except CreditBalanceExhausted:
            raise
        except Exception as e:
            log(f"  [주의] Embedding 실패 -> N-gram만 사용: {e}")
            return None
    return [v or [] for v in vecs]


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a)!=len(b): return 0.0
    dot=sum(x*y for x,y in zip(a,b)); na=math.sqrt(sum(x*x for x in a)); nb=math.sqrt(sum(y*y for y in b))
    return dot/(na*nb) if na and nb else 0.0


# ---------- Article clustering ----------
def relevant(topic: str, a: dict[str, Any]) -> bool:
    ts=tokens(topic); tnorm=normalize_compact(topic); combined=(a.get("title","")+" "+a.get("description","")).lower(); cnorm=normalize_compact(combined)
    hits=0
    for t in ts:
        tn=normalize_compact(t)
        if t.lower() in combined or (tn and tn in cnorm): hits+=1
    need=min(2,len(ts)) if len(ts)>1 else 1
    return bool((tnorm and tnorm in cnorm) or (ts and hits>=need))


def cluster(conn: sqlite3.Connection, topic: str, articles: list[dict[str, Any]]) -> dict[str, Any]:
    rel=[a for a in articles if relevant(topic,a)]
    if not rel:
        return {"relevant":[],"dominant":[],"confidence":"LOW","publishable":False,"leadTitle":"","share":0.0,"embeddingUsed":False}
    texts=[a["title"]+" "+a["description"] for a in rel]
    ev=embeddings(conn,texts); use=ev is not None
    n=len(rel); adj=[[] for _ in range(n)]; sims={}
    for i in range(n):
        for j in range(i+1,n):
            lex=jaccard(texts[i],texts[j]); sem=cosine(ev[i],ev[j]) if ev else 0.0; sims[(i,j)]=(lex,sem)
            if lex>=LEXICAL_THRESHOLD or (ev and sem>=SEMANTIC_THRESHOLD):
                adj[i].append(j); adj[j].append(i)
    visited=set(); comps=[]
    for i in range(n):
        if i in visited: continue
        q=deque([i]); visited.add(i); comp=[]
        while q:
            cur=q.popleft(); comp.append(cur)
            for nx in adj[cur]:
                if nx not in visited: visited.add(nx); q.append(nx)
        comps.append(comp)
    dom=max(comps,key=len); share=len(dom)/len(rel)
    lead=dom[0]; best=-1
    for idx in dom:
        vals=[]
        for oth in dom:
            if idx==oth: continue
            lex,sem=sims.get((min(idx,oth),max(idx,oth)),(0,0))
            vals.append((0.35*min(1,lex/0.15)+0.65*sem) if ev else lex)
        avg=sum(vals)/len(vals) if vals else 0
        if avg>best or (abs(avg-best)<1e-9 and len(rel[idx]["title"])<len(rel[lead]["title"])):
            best,lead=avg,idx
    if len(dom)>=3 and share>=0.60: conf,pub="HIGH",True
    elif len(dom)>=2: conf,pub="MEDIUM",True
    else: conf,pub="LOW",False
    return {"relevant":rel,"dominant":[rel[i] for i in dom],"confidence":conf,"publishable":pub,"leadTitle":clean_headline(rel[lead]["title"]),"share":round(share,2),"embeddingUsed":use}


def select_fresh(articles: list[dict[str, Any]], run_now: datetime, min_count: int,
                 hour_windows: tuple[int, ...] = EVIDENCE_HOUR_WINDOWS) -> tuple[int,list[dict[str,Any]]]:
    parsed=[]
    for a in articles:
        dt=parse_article_date(a.get("pubDate", ""))
        if not dt: continue
        age=(run_now-dt.astimezone(run_now.tzinfo)).total_seconds()/3600
        if age>=-2: parsed.append((age,dt,a))
    parsed.sort(key=lambda x:x[1],reverse=True)
    for hours in hour_windows:
        window=[a for age,dt,a in parsed if age<=hours]
        if len(window)>=min_count: return hours,window
    return 0,[]


def dedupe_articles(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out=[]; seen=set()
    for a in items:
        k=a.get("originalLink") or a.get("link") or normalize_text(a.get("title",""))
        if k not in seen: out.append(a); seen.add(k)
    return out


NORMAL_SCHEMA='''{
  "supported": true,
  "question": "존댓말로 끝나는 자연스러운 질문",
  "whyNow": "존댓말 합니다체로 쓴 급상승의 직접 계기 한 문장",
  "whyNowMedium": "중간 폭 카드용 존댓말 합니다체 한 문장",
  "whyNowCompact": "좁은 카드용 존댓말 합니다체 한 문장",
  "summary": "존댓말 합니다체로 쓴 최대 두 문장",
  "reasonIfUnsupported": "지원 못하면 이유, 지원하면 빈 문자열",
  "supportingSourceIndexes": [1,2]
}'''
NORMAL_INST='''당신은 한국 실시간 트렌드 서비스 '갑자기왜?'의 검증 편집자입니다.
제공된 검색어와 기사 근거만 사용해 왜 지금 갑자기 뜨는지 매우 보수적으로 판단하세요.
단순 언급은 원인이 아닙니다. 불확실하면 supported=false가 우선입니다.
기사 날짜가 현재 상승 시점과 가까워야 하며, supportingSourceIndexes에는 whyNow를 직접 뒷받침하는 기사만 넣으세요.
supported=true라면 서로 다른 언론사의 직접 근거 2건 이상이 원칙입니다.

문체 규칙은 반드시 지키세요.
- 사이트에 노출되는 question, whyNow, summary는 모두 한국어 존댓말 '합니다체'로 작성하세요.
- 서술문은 반드시 '-습니다.', '-입니다.', '-했습니다.', '-됐습니다.'처럼 정중하게 끝내세요.
- '-다.', '-했다.', '-됐다.', '-이다.', '-커졌다.' 같은 평서형 반말 종결은 사용하지 마세요.
- question도 '왜 화제인가요?', '왜 주목받고 있나요?'처럼 정중한 질문형으로 끝내세요.
- 기사 제목을 인용하는 경우를 제외하면 반말 문장을 만들지 마세요.

whyNow 길이 규칙:
- 사진 직전까지 이어지는 넓은 두 줄 인사이트 영역을 충분히 채우는 문장이어야 합니다.
- 핵심 원인과 결과뿐 아니라 기사에서 교차검증된 구체적 맥락이나 영향을 함께 담으세요.
- 넓은 화면용 기본 문장의 목표 시각 폭은 한국어 기준 약 86~92자 폭입니다.
- 불필요한 군더더기로 늘리지 말고, 근거 기사에 실제로 있는 날짜·수치·장소·후속 영향 중 유용한 내용을 보충하세요.
- 문장은 반드시 한 문장으로 끝내세요.
- summary와 달리 whyNow에는 배경 설명을 길게 넣지 마세요.
- 같은 사실관계를 유지하면서 whyNowMedium은 약 70~76자 폭, whyNowCompact는 약 46~52자 폭으로 함께 작성하세요.'''


POLITE_SCHEMA='''{
  "question": "존댓말 질문",
  "whyNow": "존댓말 합니다체 문장",
  "summary": "존댓말 합니다체 문장"
}'''
POLITE_INST='''다음 JSON의 사실관계와 의미는 절대 바꾸지 말고 문체만 교정하세요.
question은 반드시 존댓말 질문형으로, whyNow와 summary의 모든 서술문은 반드시 한국어 합니다체로 작성하세요.
'-다.', '-했다.', '-됐다.', '-이다.' 같은 평서형 반말 종결은 사용하지 마세요.
새로운 사실을 추가하거나 기존 사실을 삭제하지 마세요.'''

def _needs_polite_fix(ai: dict[str, Any]) -> bool:
    q=str(ai.get("question") or "").strip()
    if q and q.endswith("?") and not re.search(r"(요|까요|나요|인가요|습니까|일까요|건가요)\?$", q):
        return True
    for field in ("whyNow","summary"):
        text=str(ai.get(field) or "").strip()
        if not text:
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", text):
            s=sentence.strip()
            if s.endswith(".") and not s.endswith("니다."):
                return True
    return False

def ensure_polite_output(ai: dict[str, Any]) -> dict[str, Any]:
    if not _needs_polite_fix(ai):
        return ai
    try:
        revised=openai_json(
            POLITE_INST,
            json.dumps({
                "question":str(ai.get("question") or ""),
                "whyNow":str(ai.get("whyNow") or ""),
                "summary":str(ai.get("summary") or "")
            },ensure_ascii=False),
            POLITE_SCHEMA
        )
        out=dict(ai)
        for key in ("question","whyNow","summary"):
            if str(revised.get(key) or "").strip():
                out[key]=str(revised[key]).strip()
        return out
    except CreditBalanceExhausted:
        raise
    except Exception:
        return ai


WHY_SHAPE_SCHEMA='''{
  "whyNow": "넓은 카드용 존댓말 합니다체 한 문장",
  "whyNowMedium": "중간 폭 카드용 존댓말 합니다체 한 문장",
  "whyNowCompact": "좁은 카드용 존댓말 합니다체 한 문장"
}'''
WHY_SHAPE_INST='''다음 whyNow 문장을 화면 폭에 맞는 세 가지 한 줄 문장으로 다듬으세요.

목표:
- whyNow는 넓은 화면에서 사진 직전까지 두 줄을 채우며 목표 시각 폭은 86~92, 가장 좋은 폭은 89입니다.
- whyNowMedium은 중간 화면에서 사진 직전까지 두 줄을 채우며 목표 시각 폭은 70~76, 가장 좋은 폭은 73입니다.
- whyNowCompact는 좁은 화면용이며 목표 시각 폭은 46~52, 가장 좋은 폭은 49입니다.
- 세 문장 모두 각 화면의 카드 오른쪽 썸네일 직전까지 자연스럽게 차도록 작성하세요.
- 다만 억지로 늘리거나 군더더기를 붙이지 마세요.

규칙:
- 세 문장의 사실관계와 핵심 원인은 같아야 합니다.
- 세 문장 모두 한국어 존댓말 합니다체 한 문장으로 끝내세요.
- 넓은 화면용 문장에는 summary에 있는 구체적 날짜·수치·장소·배경·후속 영향 중 원인 이해에 도움이 되는 내용을 보충하세요.
- 중간 화면용 문장은 핵심 원인과 가장 중요한 구체적 맥락을 유지하세요.
- 짧은 화면용 문장에서는 부차적인 수치·날짜·배경을 먼저 덜어내세요.
- summary에 직접 적힌 사실만 보충할 수 있습니다.
- 입력에 없는 사실을 만들어야만 길이를 맞출 수 있다면 짧은 문장을 유지하세요.
- 날짜, 반복 표현, '관련 보도가', '관심이', '주목을 받았습니다' 같은 부가 표현은 핵심이 아니면 빼세요.
- 새로운 사실은 추가하지 마세요.
- 세 문장 모두 줄바꿈 없이 출력하세요.'''

def visual_text_units(text: str) -> float:
    # Pretendard 14px 카드 문장을 위한 대략적 시각 폭 단위
    import unicodedata
    total=0.0
    for ch in str(text or ""):
        if ch.isspace():
            total += 0.34
        elif unicodedata.east_asian_width(ch) in ("W","F"):
            total += 1.0
        elif ch.isascii() and ch.isalnum():
            total += 0.56
        else:
            total += 0.50
    return total

def _shape_penalty(text: str, lo: float, hi: float, target: float) -> float:
    units=visual_text_units(text)
    outside=max(lo-units,units-hi,0.0)
    return outside*6.0+abs(units-target)


def shape_why_now(ai: dict[str, Any], min_units: float = 86.0, max_units: float = 92.0,
                  medium_min: float = 70.0, medium_max: float = 76.0,
                  compact_min: float = 46.0, compact_max: float = 52.0) -> dict[str, Any]:
    why=str(ai.get("whyNow") or "").strip()
    if not why:
        return ai
    units=visual_text_units(why)
    medium=str(ai.get("whyNowMedium") or "").strip()
    medium_units=visual_text_units(medium) if medium else 0.0
    compact=str(ai.get("whyNowCompact") or "").strip()
    compact_units=visual_text_units(compact) if compact else 0.0
    if min_units<=units<=max_units and medium and medium_min<=medium_units<=medium_max and compact and compact_min<=compact_units<=compact_max:
        return ai
    candidates=[]
    if medium and compact:
        candidates.append((why,medium,compact))
    previous=None
    for attempt in range(1):
        try:
            revised=openai_json(
                WHY_SHAPE_INST,
                json.dumps({
                    "originalWhyNow": why,
                    "summary": str(ai.get("summary") or ""),
                    "question": str(ai.get("question") or ""),
                    "originalVisualUnits": round(units,1),
                    "targets":{"whyNow":"86~92 (best 89)","whyNowMedium":"70~76 (best 73)","whyNowCompact":"46~52 (best 49)"},
                    "previousAttempt": previous,
                    "attempt": attempt+1
                },ensure_ascii=False),
                WHY_SHAPE_SCHEMA
            )
            shaped=str(revised.get("whyNow") or "").strip()
            shaped_medium=str(revised.get("whyNowMedium") or "").strip()
            shaped_compact=str(revised.get("whyNowCompact") or "").strip()
            previous={"whyNow":shaped,"whyNowMedium":shaped_medium,"whyNowCompact":shaped_compact,
                      "whyNowUnits":round(visual_text_units(shaped),1),
                      "whyNowMediumUnits":round(visual_text_units(shaped_medium),1),
                      "whyNowCompactUnits":round(visual_text_units(shaped_compact),1)}
            if shaped and shaped_medium and shaped_compact:
                candidates.append((shaped,shaped_medium,shaped_compact))
                su=visual_text_units(shaped); mu=visual_text_units(shaped_medium); cu=visual_text_units(shaped_compact)
                if min_units<=su<=max_units and medium_min<=mu<=medium_max and compact_min<=cu<=compact_max:
                    break
        except CreditBalanceExhausted:
            raise
        except Exception:
            break
    if not candidates:
        out=dict(ai); out["whyNowMedium"]=why; out["whyNowCompact"]=why; return out
    shaped,shaped_medium,shaped_compact=min(
        candidates,
        key=lambda trio:_shape_penalty(trio[0],min_units,max_units,89.0)
        +_shape_penalty(trio[1],medium_min,medium_max,73.0)
        +_shape_penalty(trio[2],compact_min,compact_max,49.0)
        +(100.0 if visual_text_units(trio[1])>visual_text_units(trio[0]) else 0.0)
        +(100.0 if visual_text_units(trio[2])>visual_text_units(trio[1]) else 0.0)
    )
    out=dict(ai); out["whyNow"]=shaped; out["whyNowMedium"]=shaped_medium; out["whyNowCompact"]=shaped_compact
    return out


def validate_normal(conn: sqlite3.Connection, item: dict[str, Any], cl: dict[str, Any], run_now: datetime) -> dict[str, Any]:
    topic=item["topic"]; rank=item["rank"]
    base={"rank":rank,"topic":topic,"score":item["suddenlyWhyScore"],"ruleConfidence":cl["confidence"]}
    if not cl["publishable"]:
        return {**base,"evidenceWindowHours":0,"aiSupported":False,"publishable":False,"cacheHit":False,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"현재 공개된 기사만으로는 검색 급증 원인을 하나로 특정하기 어렵습니다.","summary":"관련 이슈가 여러 갈래로 흩어져 있거나 직접적인 기사 근거가 부족합니다.","reasonIfUnsupported":"Rule-based validator marked this topic as LOW/NOISE.","sources":[]}
    window,evidence=select_fresh(cl["dominant"],run_now,2)
    if len(evidence)<2:
        return {**base,"evidenceWindowHours":0,"aiSupported":False,"publishable":False,"cacheHit":False,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"최근 72시간 안에 검색 급증 원인을 뒷받침하는 독립적인 기사 근거가 충분하지 않습니다.","summary":"관련 기사가 있더라도 현재 검색 상승 시점과 거리가 있어 원인으로 단정하지 않습니다.","reasonIfUnsupported":"Fewer than two corroborating cluster articles were found within the last 72 hours.","sources":[]}
    sources=[]
    for i,a in enumerate(evidence,1):
        sources.append({"sourceIndex":i,"title":clean_headline(a["title"]),"description":clean_html(str(a.get("description") or "")),"pubDate":a.get("pubDate",""),"link":a.get("link",""),"originalLink":a.get("originalLink",""),"publisherHost":source_host(a.get("originalLink",""),a.get("link",""))})
    fp=sha256_text("normal-v8.4\n"+FAST_MODEL+"\n"+topic+"\n"+str(window)+"\n"+"\n".join(sorted(f"{a['title']}|{a.get('pubDate','')}|{a.get('description','')}" for a in evidence)))
    ai=cache_get(conn,fp); hit=ai is not None
    if ai is None:
        context={"topic":topic,"suddenlyWhyScore":item["suddenlyWhyScore"],"ruleConfidence":cl["confidence"],"dominantClusterCount":len(cl["dominant"]),"dominantClusterShare":cl["share"],"embeddingUsed":cl["embeddingUsed"],"runTime":run_now.isoformat(),"evidenceWindowHours":window,"evidenceArticles":[{"sourceIndex":s["sourceIndex"],"publisherHost":s["publisherHost"],"title":a["title"],"description":a.get("description",""),"pubDate":a.get("pubDate","")} for s,a in zip(sources,evidence)]}
        ai=openai_json(NORMAL_INST,"다음 트렌드를 검증하세요.\n\n"+json.dumps(context,ensure_ascii=False),NORMAL_SCHEMA)
        ai=ensure_polite_output(ai)
        ai=shape_why_now(ai)
        cache_put(conn,fp,"normal",ai)
    idx=[]
    for x in ai.get("supportingSourceIndexes") or []:
        try:
            ix=int(x)
            if 1<=ix<=len(sources) and ix not in idx: idx.append(ix)
        except Exception: pass
    selected=[sources[i-1] for i in idx]; pubs={s["publisherHost"] for s in selected if s["publisherHost"]}; ais=bool(ai.get("supported")); valid=ais and len(selected)>=2 and len(pubs)>=2
    reason=str(ai.get("reasonIfUnsupported") or "")
    if ais and not valid: reason="AI가 원인을 제시했지만 서로 다른 언론사의 직접 근거 기사 2건 이상이 확인되지 않았습니다."
    return {**base,"evidenceWindowHours":window,"aiSupported":ais,"publishable":valid,"cacheHit":hit,"supportingSourceCount":len(selected),"independentPublisherCount":len(pubs),"question":str(ai.get("question") or f"왜 '{topic}' 검색이 갑자기 늘었을까?"),"whyNow":str(ai.get("whyNow") or "") if valid else "현재 기사 근거만으로는 검색 급증 원인을 충분히 교차검증하지 못했습니다.","whyNowMedium":str(ai.get("whyNowMedium") or ai.get("whyNow") or "") if valid else "현재 기사 근거만으로는 원인을 충분히 검증하지 못했습니다.","whyNowCompact":str(ai.get("whyNowCompact") or ai.get("whyNowMedium") or ai.get("whyNow") or "") if valid else "현재 기사 근거만으로는 원인을 충분히 검증하지 못했습니다.","summary":str(ai.get("summary") or "") if valid else "직접적인 근거 기사와 독립적인 출처가 더 필요해 원인 설명을 게시하지 않습니다.","reasonIfUnsupported":reason,"sources":selected}


BROAD_PLAN_SCHEMA='''{"rescueCandidate":true,"scopeType":"specific_entity | broad_entity | category | ranking | schedule | ambiguous | other","aliases":["동의어"],"newsQueries":["짧은 뉴스 검색어"],"behaviorHint":"none | standings_after_games | schedule_before_event | recurring_update | general_attention","rationale":"판단 이유"}'''
BROAD_PLAN_INST='''당신은 한국 실시간 검색 트렌드의 검색어 확장 플래너입니다.
기존 분석에서 원인을 찾지 못한 검색어를 현재의 구체적 하위 이슈로 풀어 뉴스 검색어를 설계하세요.
입력에 최근 뉴스 후보가 있으면 제목과 요약 속 발표·논란·경기·정책 같은 직접 계기 단서를 우선하세요.
상시 정보형 검색어만 나열하지 말고, 지금의 급상승 원인을 찾을 수 있는 고유명사와 핵심 사건 표현을 포함하세요.
'배우'는 category, '야구 순위'는 ranking, '크보/KBO'는 broad_entity, 팀명도 broad_entity가 될 수 있습니다.
동명이의는 ambiguous. newsQueries는 최대 6개. 너무 넓어 구조 가치가 없으면 rescueCandidate=false.'''
BROAD_EXPLAIN_SCHEMA='''{"supported":true,"explanationType":"event | behavior | unresolved","resolvedTopic":"구체화 주제","question":"존댓말 질문","whyNow":"넓은 카드용 존댓말 합니다체 한 문장","whyNowMedium":"중간 폭 카드용 존댓말 합니다체 한 문장","whyNowCompact":"좁은 카드용 존댓말 합니다체 한 문장","summary":"존댓말 합니다체 최대 두 문장","reasonIfUnsupported":"이유","supportingSourceIndexes":[1,2]}'''
BROAD_EXPLAIN_INST='''당신은 갑자기왜?의 포괄 키워드 전문 편집자입니다.
event는 최근 구체적 사건/경기/발표가 원인인 유형입니다.
behavior는 경기 종료 후 순위 확인처럼 단일 기사보다 반복 행동이 검색을 만드는 유형입니다.
behavior는 복수 독립 기사로 현재 맥락이 명확할 때만 추정형으로 설명하세요.
여러 무관 사건이 섞이면 unresolved. '배우'처럼 여러 인물이 흩어지면 억지로 하나를 선택하지 마세요.
supported=true라면 서로 다른 언론사 직접 근거 2건 이상이 원칙입니다.

문체 규칙은 반드시 지키세요.
- question, whyNow, summary는 모두 한국어 존댓말 '합니다체'로 작성하세요.
- 서술문은 반드시 '-습니다.', '-입니다.', '-했습니다.', '-됐습니다.'처럼 정중하게 끝내세요.
- '-다.', '-했다.', '-됐다.', '-이다.' 같은 평서형 반말 종결은 사용하지 마세요.
- question은 반드시 '...인가요?', '...하나요?', '...까요?'처럼 존댓말 질문으로 작성하세요.

whyNow 길이 규칙:
- 사진 직전까지 이어지는 넓은 두 줄 영역을 충분히 채우는 문장이어야 합니다.
- 핵심 사건/행동 원인과 함께 기사에서 교차검증된 구체적 맥락이나 영향을 담으세요.
- 넓은 화면용 기본 문장의 목표 시각 폭은 약 86~92입니다.
- 불필요한 반복으로 늘리지 말고 기사에 실제로 있는 날짜·수치·장소·후속 영향 중 유용한 내용을 활용하세요.
- 반드시 한 문장으로 끝내세요.
- 같은 사실관계를 유지하면서 whyNowMedium은 약 70~76자 폭, whyNowCompact는 약 46~52자 폭으로 함께 작성하세요.'''


def broad_plan(conn: sqlite3.Connection, item: dict[str,Any], seed_articles: list[dict[str,Any]]) -> tuple[dict[str,Any],bool]:
    topic=str(item["topic"])
    clues=[{"title":a.get("title",""),"description":a.get("description",""),"pubDate":a.get("pubDate","")} for a in seed_articles[:12]]
    clue_fp="\n".join(sorted(f"{x['pubDate']}|{x['title']}|{x['description']}" for x in clues))
    key=sha256_text("broad-plan-v8.4|"+FAST_MODEL+"|"+topic.lower()+"|"+clue_fp); cached=cache_get(conn,key)
    if cached is not None: return cached,True
    context={"topic":topic,"observedTitles":item.get("observedTitles",""),"firstSeen":item.get("firstSeen",""),"lastSeen":item.get("lastSeen",""),"recentNewsCandidates":clues}
    plan=openai_json(BROAD_PLAN_INST,json.dumps(context,ensure_ascii=False),BROAD_PLAN_SCHEMA)
    plan.setdefault("rescueCandidate",True); plan.setdefault("scopeType","other"); plan["aliases"]=unique(plan.get("aliases") or [],5); plan["newsQueries"]=unique(plan.get("newsQueries") or [],6); plan.setdefault("behaviorHint","none"); plan.setdefault("rationale","")
    cache_put(conn,key,"broad_plan",plan); return plan,False


def broad_rescue(conn: sqlite3.Connection,item:dict[str,Any],normal:dict[str,Any],run_now:datetime,
                 seed_articles:list[dict[str,Any]],max_evidence_hours:int=72)->tuple[dict[str,Any],dict[str,Any]]:
    topic=item["topic"]
    try: plan,phit=broad_plan(conn,item,seed_articles)
    except CreditBalanceExhausted:
        raise
    except Exception as e:
        rescue={"rank":item["rank"],"topic":topic,"attempted":True,"publishable":False,"scopeType":"other","explanationType":"unresolved","evidenceWindowHours":0,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"포괄 키워드 재분석 중 오류가 발생했습니다.","summary":"원인 설명을 게시하지 않습니다.","reasonIfUnsupported":str(e),"sources":[],"queryPlan":[]}; return normal,rescue
    useful_plan_queries=unique(list(plan.get("aliases") or [])+list(plan.get("newsQueries") or []),6)
    # V7.1: planner가 너무 넓다고 판단했더라도 실제 하위 검색어를 만들었다면
    # 한 번은 뉴스 확장 검색을 해본다. 우선주 같은 범주형 검색어를 살릴 기회를 남긴다.
    if not bool(plan.get("rescueCandidate",True)) and not useful_plan_queries:
        rescue={"rank":item["rank"],"topic":topic,"attempted":True,"publishable":False,"scopeType":plan.get("scopeType","other"),"explanationType":"unresolved","evidenceWindowHours":0,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"검색어 범위가 넓거나 모호해 현재 근거만으로는 원인을 특정하기 어렵습니다.","summary":plan.get("rationale",""),"reasonIfUnsupported":plan.get("rationale",""),"sources":[],"queryPlan":[]}; return normal,rescue
    queries=unique([topic]+list(plan.get("aliases") or [])+list(plan.get("newsQueries") or []),8); collected=list(seed_articles)
    for q in queries:
        if normalize_text(q)==normalize_text(topic):
            continue
        try: collected+=naver_news(q,BROAD_NEWS_PER_QUERY,"sim")
        except CreditBalanceExhausted:
            raise
        except Exception as e: log(f"  [주의] 확장 뉴스 실패 {q}: {e}")
        time.sleep(0.06)
    collected=dedupe_articles(collected); parsed=[]
    for a in collected:
        dt=parse_article_date(a.get("pubDate",""))
        if dt:
            age=(run_now-dt.astimezone(run_now.tzinfo)).total_seconds()/3600
            if -2<=age<=max_evidence_hours: parsed.append((age,dt,a))
    parsed.sort(key=lambda x:((x[2].get("searchSort") in {"sim","verified-history"}),x[1]),reverse=True)
    fresh=[]; per_host=defaultdict(int)
    for age,dt,a in parsed:
        host=str(a.get("publisherHost") or "")
        if host and per_host[host]>=3: continue
        fresh.append(a)
        if host: per_host[host]+=1
        if len(fresh)>=BROAD_MAX_ARTICLES: break
    window=max_evidence_hours if fresh else 0
    hosts={a.get("publisherHost","") for a in fresh if a.get("publisherHost")}
    if len(fresh)<4 or len(hosts)<2:
        rescue={"rank":item["rank"],"topic":topic,"attempted":True,"publishable":False,"scopeType":plan.get("scopeType","other"),"explanationType":"unresolved","evidenceWindowHours":window,"supportingSourceCount":0,"independentPublisherCount":len(hosts),"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"확장 검색까지 했지만 최근 기사 근거가 충분히 모이지 않았습니다.","summary":"동의어와 하위 검색어까지 넓혀 확인했지만 원인을 교차검증할 정도의 최근 근거가 부족합니다.","reasonIfUnsupported":"Insufficient fresh expanded evidence.","sources":[],"queryPlan":queries}; return normal,rescue
    prompt=[{"sourceIndex":i+1,"query":a.get("query",""),"publisherHost":a.get("publisherHost",""),"pubDate":a.get("pubDate",""),"title":a.get("title",""),"description":a.get("description","")} for i,a in enumerate(fresh)]
    fp=sha256_text("broad-explain-v8.4|"+FAST_MODEL+"|"+topic+"|"+str(window)+"|"+"\n".join(sorted(f"{a.get('publisherHost','')}|{a.get('pubDate','')}|{a.get('title','')}|{a.get('description','')}" for a in fresh))); ai=cache_get(conn,fp); ehit=ai is not None
    if ai is None:
        ctx={"topic":topic,"scopeType":plan.get("scopeType","other"),"behaviorHint":plan.get("behaviorHint","none"),"runTime":run_now.isoformat(),"evidenceWindowHours":window,"searchQueries":queries,"articles":prompt}
        try:
            ai=openai_json(BROAD_EXPLAIN_INST,"다음 포괄 검색어를 재분석하세요.\n\n"+json.dumps(ctx,ensure_ascii=False),BROAD_EXPLAIN_SCHEMA)
            ai=ensure_polite_output(ai)
            ai=shape_why_now(ai)
            cache_put(conn,fp,"broad_explain",ai)
        except CreditBalanceExhausted:
            raise
        except Exception as e:
            rescue={"rank":item["rank"],"topic":topic,"attempted":True,"publishable":False,"scopeType":plan.get("scopeType","other"),"explanationType":"unresolved","evidenceWindowHours":window,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"포괄 키워드 재분석 중 오류가 발생했습니다.","summary":"원인 설명을 게시하지 않습니다.","reasonIfUnsupported":str(e),"sources":[],"queryPlan":queries}; return normal,rescue
    idx=[]
    for x in ai.get("supportingSourceIndexes") or []:
        try:
            ix=int(x)
            if 1<=ix<=len(fresh) and ix not in idx: idx.append(ix)
        except Exception: pass
    sources=[]
    for ix in idx:
        a=fresh[ix-1]; sources.append({"sourceIndex":ix,"title":a.get("title",""),"description":clean_html(str(a.get("description") or "")),"pubDate":a.get("pubDate",""),"link":a.get("link",""),"originalLink":a.get("originalLink",""),"publisherHost":a.get("publisherHost",""),"query":a.get("query","")})
    pubs={s["publisherHost"] for s in sources if s["publisherHost"]}; et=str(ai.get("explanationType") or "unresolved"); valid=bool(ai.get("supported")) and et!="unresolved" and len(sources)>=2 and len(pubs)>=2
    rescue={"rank":item["rank"],"topic":topic,"attempted":True,"publishable":valid,"scopeType":plan.get("scopeType","other"),"explanationType":et,"resolvedTopic":str(ai.get("resolvedTopic") or topic),"evidenceWindowHours":window,"supportingSourceCount":len(sources),"independentPublisherCount":len(pubs),"question":str(ai.get("question") or f"왜 '{topic}' 검색이 갑자기 늘었을까?"),"whyNow":str(ai.get("whyNow") or "") if valid else "확장 검색까지 했지만 검색 급증 원인을 충분히 교차검증하지 못했습니다.","whyNowMedium":str(ai.get("whyNowMedium") or ai.get("whyNow") or "") if valid else "확장 검색에서도 원인을 충분히 검증하지 못했습니다.","whyNowCompact":str(ai.get("whyNowCompact") or ai.get("whyNowMedium") or ai.get("whyNow") or "") if valid else "확장 검색에서도 원인을 충분히 검증하지 못했습니다.","summary":str(ai.get("summary") or "") if valid else str(ai.get("reasonIfUnsupported") or ""),"reasonIfUnsupported":str(ai.get("reasonIfUnsupported") or ""),"sources":sources,"queryPlan":queries,"planCacheHit":phit,"explainCacheHit":ehit}
    if not valid: return normal,rescue
    merged={"rank":item["rank"],"topic":topic,"resolvedTopic":rescue["resolvedTopic"],"score":item["suddenlyWhyScore"],"ruleConfidence":"BROAD-RESCUE","resolutionMode":"broad","explanationType":et,"evidenceWindowHours":window,"aiSupported":True,"publishable":True,"cacheHit":ehit,"supportingSourceCount":len(sources),"independentPublisherCount":len(pubs),"question":rescue["question"],"whyNow":rescue["whyNow"],"whyNowMedium":rescue["whyNowMedium"],"whyNowCompact":rescue["whyNowCompact"],"summary":rescue["summary"],"reasonIfUnsupported":"","sources":sources}
    return merged,rescue


FINAL_VERIFY_SCHEMA='''{
  "approved": true,
  "displayTopic": "사용자가 사건을 바로 이해할 수 있는 구체적인 제목",
  "question": "근거에 맞게 다듬은 질문",
  "whyNow": "근거에 맞게 다듬은 넓은 화면용 설명",
  "whyNowMedium": "같은 사실의 중간 폭 설명",
  "whyNowCompact": "같은 사실의 모바일용 설명",
  "reasonIfRejected": "거절 사유 또는 빈 문자열"
}'''
FINAL_VERIFY_INST='''당신은 '갑자기왜?'의 최종 사실 검증 편집자입니다.
선택된 각 근거 기사의 제목, description(기사 검색 요약), 날짜를 모두 근거로 사용해 설명을 엄격하게 확인하세요.
제목에 없더라도 description에 명시된 사실은 근거가 있는 것으로 판단하세요.
핵심 급상승 원인이 서로 다른 언론사 2곳 이상의 근거로 확인되면 approved=true로 하세요.
다만 설명 속 일부 수치, 날짜, 장소나 부가 표현만 근거가 약하면 후보 전체를 거절하지 말고 그 표현을 삭제하거나 완화한 question과 whyNow 3종을 반환하세요.
핵심 원인 자체가 뒷받침되지 않거나, 기사끼리 모순되거나, 현재 급상승과 무관한 오래된 사건일 때만 approved=false입니다.
반환하는 문장에는 기사에 없는 사실을 새로 만들지 말고 자연스러운 존댓말 문장으로 작성하세요. whyNow 3종은 길이만 다르고 사실은 같아야 합니다.
displayTopic은 검색어를 그대로 반복하지 말고 이번 급상승 사건을 한눈에 알 수 있는 짧고 구체적인 한국어 제목으로 작성하세요.
예를 들어 원 검색어가 '수력 발전'이고 근거가 네팔 수력발전소 홍수라면 '네팔 수력발전소 홍수'처럼 씁니다.
사람 이름이나 작품명처럼 원 검색어 자체가 이미 구체적이면 그대로 유지해도 됩니다.'''


def evidence_fingerprint(topic: str, articles: list[dict[str, Any]]) -> str:
    rows=[]
    for article in articles[:12]:
        rows.append("|".join((
            normalize_text(clean_headline(str(article.get("title") or ""))),
            normalize_text(str(article.get("publisherHost") or "")),
            normalize_text(str(article.get("pubDate") or "")),
        )))
    return sha256_text("analysis-v8.5|"+topic_dedupe_key(topic)+"|"+"\n".join(sorted(rows)))


def latest_reusable_item(conn: sqlite3.Connection, topic: str, fingerprint: str,
                         run_now: datetime, max_age_hours: int = 36) -> dict[str, Any] | None:
    rows=conn.execute(
        "SELECT generated_at,item_json FROM ranking_history WHERE dedupe_key=? AND publishable=1 "
        "ORDER BY datetime(generated_at) DESC LIMIT 12",
        (topic_dedupe_key(topic),),
    ).fetchall()
    for row in rows:
        try:
            generated=datetime.fromisoformat(str(row["generated_at"]))
            if generated.tzinfo is None:
                generated=generated.replace(tzinfo=run_now.tzinfo)
            age=(run_now-generated.astimezone(run_now.tzinfo)).total_seconds()/3600
            item=json.loads(row["item_json"])
        except Exception:
            continue
        if age < -1 or age > max_age_hours:
            continue
        if item.get("analysisFingerprint") != fingerprint:
            continue
        if item.get("finalVerifiedModel") != VERIFY_MODEL:
            continue
        if not item.get("publishable"):
            continue
        return item
    return None


def final_verify(conn: sqlite3.Connection, item: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    sources=list(item.get("sources") or [])
    context={
        "searchTopic":item.get("topic"),
        "resolvedTopic":item.get("resolvedTopic"),
        "question":item.get("question"),
        "whyNow":item.get("whyNow"),
        "sources":[{
            "publisherHost":s.get("publisherHost"),
            "pubDate":s.get("pubDate"),
            "title":s.get("title"),
            "description":str(s.get("description") or "")[:500],
        } for s in sources],
    }
    key=sha256_text("final-gate-v8.5|"+VERIFY_MODEL+"|"+json.dumps(context,ensure_ascii=False,sort_keys=True))
    checked=cache_get(conn,key); hit=checked is not None
    if checked is None:
        checked=openai_json(
            FINAL_VERIFY_INST,
            "다음 게시 후보를 최종 검증하세요.\n\n"+json.dumps(context,ensure_ascii=False),
            FINAL_VERIFY_SCHEMA,
            model=VERIFY_MODEL,
        )
        cache_put(conn,key,"final_verify",checked)
    approved=bool(checked.get("approved"))
    out=dict(item)
    out["finalVerifiedModel"]=VERIFY_MODEL
    out["finalVerifyCacheHit"]=hit
    out["displayTopic"]=str(checked.get("displayTopic") or item.get("resolvedTopic") or item.get("topic") or "").strip()
    if approved:
        for field in ("question", "whyNow", "whyNowMedium", "whyNowCompact"):
            revised=str(checked.get(field) or "").strip()
            if revised:
                out[field]=revised
    if not approved:
        out["publishable"]=False
        out["reasonIfUnsupported"]=str(checked.get("reasonIfRejected") or "최종 검증에서 근거와 설명의 직접 연결이 충분하지 않았습니다.")
    return out,approved


def og_image_from_url(conn: sqlite3.Connection, url: str) -> str:
    """기사 한 건의 대표 이미지를 캐시와 HTML 메타 태그에서 찾는다."""
    url=str(url or "").strip()
    if not url:
        return ""
    key = sha256_text("og-image-v82|" + url)
    cached = cache_get(conn, key, max_days=30)
    if isinstance(cached, dict) and "url" in cached:
        return str(cached.get("url") or "")
    image = ""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36 SuddenlyWhy/8.3",
            "Accept":"text/html,*/*"
        })
        with urllib.request.urlopen(req, timeout=6) as r:
            raw = r.read(700_000).decode("utf-8", errors="ignore")
        pats = [
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']',
            r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image["\']',
            r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)',
            r'<link[^>]+href=["\']([^"\']+)["\'][^>]+rel=["\']image_src["\']',
        ]
        for pat in pats:
            m = re.search(pat, raw, flags=re.I)
            if m:
                image = html.unescape(m.group(1).strip())
                image = urllib.parse.urljoin(url, image)
                break
    except Exception:
        image = ""
    cache_put(conn, key, "og_image", {"url":image})
    return image


def article_thumbnail(conn: sqlite3.Connection, sources: list[dict[str,Any]]) -> tuple[str,str]:
    """대표 이미지와 그 이미지를 찾은 기사 주소를 함께 반환한다."""
    seen=set(); checked=0
    for source in sources:
        for candidate in (source.get("originalLink"),source.get("link")):
            url=str(candidate or "").strip()
            if not url or url in seen: continue
            seen.add(url); checked+=1
            image=og_image_from_url(conn,url)
            if image: return image,url
            if checked>=6: return "",""
    return "",""


def add_site_fields(conn: sqlite3.Connection, item: dict[str,Any], raw_rank: int, display_rank: int) -> dict[str,Any]:
    out = dict(item)
    out["rawRank"] = int(raw_rank)
    out["rank"] = int(display_rank)
    if not out.get("thumbnail"):
        thumbnail,source_link = article_thumbnail(conn, list(out.get("sources") or []))
        out["thumbnail"] = thumbnail
        out["thumbnailSourceLink"] = source_link
    elif not out.get("thumbnailSourceLink"):
        for source in out.get("sources") or []:
            source_link=str(source.get("originalLink") or source.get("link") or "").strip()
            if source_link:
                out["thumbnailSourceLink"]=source_link
                break
    return out


# ---------- History ----------
def save_history(conn:sqlite3.Connection,run_now:datetime,items:list[dict[str,Any]])->int:
    cur=conn.execute("INSERT INTO runs(generated_at,version,model) VALUES(?,?,?)",(run_now.isoformat(),VERSION,MODEL)); rid=int(cur.lastrowid)
    rows=[]
    for x in items:
        rows.append((
            rid,run_now.isoformat(),int(x["rank"]),int(x.get("rawRank") or x["rank"]),str(x["topic"]),
            topic_dedupe_key(str(x["topic"])),float(x.get("score") or 0),1 if x.get("publishable") else 0,
            str(x.get("explanationType") or ""),str(x.get("question") or ""),str(x.get("whyNow") or ""),
            str(x.get("summary") or ""),json.dumps(x,ensure_ascii=False)
        ))
    conn.executemany("INSERT INTO ranking_history(run_id,generated_at,rank,raw_rank,topic,dedupe_key,score,publishable,explanation_type,question,why_now,summary,item_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",rows)
    conn.commit(); return rid


def period_leaderboard(conn:sqlite3.Connection,run_now:datetime,hours:int,limit:int=10)->dict[str,Any]:
    cutoff=(run_now-timedelta(hours=hours)).isoformat()
    rows=conn.execute("SELECT generated_at,rank,raw_rank,topic,dedupe_key,score,publishable,explanation_type,question,why_now,summary,item_json FROM ranking_history WHERE datetime(generated_at)>=datetime(?) ORDER BY datetime(generated_at),rank",(cutoff,)).fetchall()
    snapshots=sorted({r["generated_at"] for r in rows})
    snapshot_count=len(snapshots)
    grouped={}
    for r in rows:
        key=r["dedupe_key"]
        g=grouped.setdefault(key,{"topic":r["topic"],"appearances":0,"rankSum":0.0,"scoreSum":0.0,"bestRank":999,"latestTime":"","latestItem":None})
        g["appearances"]+=1; g["rankSum"]+=float(r["rank"]); g["scoreSum"]+=float(r["score"] or 0); g["bestRank"]=min(g["bestRank"],int(r["rank"]))
        if r["generated_at"]>=g["latestTime"]:
            g["latestTime"]=r["generated_at"]
            try: g["latestItem"]=json.loads(r["item_json"]) if r["item_json"] else dict(r)
            except Exception: g["latestItem"]=dict(r)
    scored=[]
    for g in grouped.values():
        avg_rank=g["rankSum"]/max(1,g["appearances"]); avg_score=g["scoreSum"]/max(1,g["appearances"])
        persistence=(g["appearances"]/max(1,snapshot_count))*100.0
        rank_quality=max(0.0,(11.0-avg_rank)/10.0*100.0)
        period_score=0.60*persistence+0.25*rank_quality+0.15*avg_score
        item=dict(g["latestItem"] or {})
        item["periodStats"]={"appearances":g["appearances"],"snapshotCount":snapshot_count,"bestRank":g["bestRank"],"averageRank":round(avg_rank,1),"averageScore":round(avg_score,1),"periodScore":round(period_score,1)}
        scored.append((period_score,g["latestTime"],item))
    scored.sort(key=lambda x:(-x[0],-x[2].get("score",0),x[2].get("rank",999)))
    rankings=[]
    for i,(_,_,item) in enumerate(scored[:limit],1):
        item["rank"]=i; rankings.append(item)
    return {"generatedAt":run_now.isoformat(),"windowHours":hours,"snapshotCount":snapshot_count,"rankings":rankings}


def export_history(conn:sqlite3.Connection,run_now:datetime,hours:int,path:Path)->None:
    json_dump(path,period_leaderboard(conn,run_now,hours,TOP_COUNT))


def main()->int:
    run_now=now_local(); signals_only="--signals-only" in sys.argv[1:]
    log("\n"+"="*72); log(" SuddenlyWhy V8.5 - evidence-aware final verification + protected final results"); log("="*72)
    log(f" 데이터: {DATA_DIR}"); log(f" GPT 초안: {FAST_MODEL} / 최종 검증: {VERIFY_MODEL}"); log(f" Embedding: {'ON '+EMBEDDING_MODEL if USE_EMBEDDINGS else 'OFF'}")
    log(f" 원시 후보: 최대 TOP{RAW_CANDIDATE_COUNT} -> 게시 가능한 TOP{TOP_COUNT}\n")
    preserve_last_good()
    previous_status=json_load(STATUS_PATH,{})
    preserve_alert=signals_only and previous_status.get("state") in {"quota_exhausted","error"}
    if not preserve_alert:
        write_status("collecting" if signals_only else "running", "무료 신호를 수집하고 있습니다." if signals_only else "새 순위를 분석하고 있습니다.", run_now)
    conn=connect_db(); imported=import_legacy_history(conn)
    if imported: log(f"[0/7] 기존 google_history.csv -> SQLite {imported}행 가져옴")
    naver_headers()
    if not signals_only:
        openai_key()

    log("[1/8] Google Trends RSS + 적응형 후보 Pool + NAVER Search Trend")
    snap=collect_google(conn,run_now)
    pool=[]; pool_hours=POOL_HOURS
    for candidate_hours in POOL_HOUR_WINDOWS:
        pool=recent_pool(conn,run_now,candidate_hours)
        pool_hours=candidate_hours
        log(f"  {candidate_hours}시간 후보: {len(pool)}개")
        if len(pool)>=RAW_CANDIDATE_COUNT:
            break
    if pool_hours>POOL_HOURS:
        log(f"  -> 24시간 후보만으로 부족해 {pool_hours}시간까지 확장합니다. 모든 후보는 최신 기사로 다시 검증합니다.")
    nmap=query_naver_trend(pool,run_now); scored=score_pool(pool,nmap,run_now)
    raw=[]
    for i,x in enumerate(scored[:RAW_CANDIDATE_COUNT],1):
        raw.append({"rank":i,"rawRank":i,**x})
        log(f"  RAW #{i:>2} {x['topic']} score={x['suddenlyWhyScore']}")
    json_dump(RAW_PATH,{"generatedAt":run_now.strftime("%Y-%m-%d %H:%M:%S"),"version":VERSION,"poolHours":pool_hours,"candidateCount":len(raw),"rankings":raw})

    if signals_only:
        conn.close()
        if preserve_alert:
            updated_status=dict(previous_status)
            updated_status["signalCollectedAt"]=run_now.strftime("%Y-%m-%d %H:%M:%S")
            updated_status["signalMessage"]="무료 검색 신호 수집은 정상 완료했습니다. 기존 오류 경고는 유지합니다."
            json_dump(STATUS_PATH,updated_status)
        else:
            write_status("signals_ok", "무료 검색 신호 수집을 완료했습니다. 마지막 정상 TOP10을 유지합니다.", run_now,
                         signalCollectedAt=run_now.strftime("%Y-%m-%d %H:%M:%S"))
        log("\n무료 Google/NAVER 신호 수집 완료 - GPT 호출 0회, 기존 TOP10 유지")
        log(f"RAW : {RAW_PATH}")
        return 0

    log("[2/8] 원시 순위대로 분석, 설명 가능한 항목 10개까지 채우기")
    final=[]; broad=[]; evidence_rows=[]; candidate_rows=[]; rejected=[]; rescued=0; analyzed=0; generic_skipped=0; reused=0; final_rejected=0

    for x in raw:
        if len(final)>=TOP_COUNT:
            break
        analyzed+=1; raw_rank=int(x["rawRank"]); topic=x["topic"]
        log(f"\n  RAW #{raw_rank} {topic}")

        if is_generic_fast_reject(topic):
            generic_skipped+=1
            reason="너무 넓은 일반 단일어라 특정 사건을 안정적으로 연결하기 어렵습니다."
            rejected.append({"rawRank":raw_rank,"topic":topic,"stage":"generic-fast-reject","reason":reason})
            broad.append({"rank":raw_rank,"rawRank":raw_rank,"topic":topic,"attempted":False,"publishable":False,"scopeType":"generic","explanationType":"unresolved","question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":reason,"summary":reason,"reasonIfUnsupported":reason,"sources":[],"queryPlan":[]})
            log("    -> 범용 단일어 빠른 제외")
            continue

        log("    1) NAVER 뉴스 관련도순 + 최신순 검색")
        try:
            searched=blended_topic_news(topic,NEWS_PER_TOPIC)
        except Exception as e:
            log(f"    [주의] 뉴스 실패: {e}"); searched=[]
        try:
            verified=prior_verified_articles(conn,topic,run_now,max(EVIDENCE_HOUR_WINDOWS))
        except Exception as e:
            log(f"    [주의] 이전 검증 기사 불러오기 실패: {e}"); verified=[]
        arts=dedupe_articles(searched+verified)
        sort_counts={kind:len([a for a in arts if a.get("searchSort")==kind]) for kind in ("sim","date","verified-history")}
        evidence_rows.append({"rawRank":raw_rank,"topic":topic,"totalFound":len(arts),"searchBreakdown":sort_counts,"articles":arts})
        log("    2) 관련성·사건 군집·임베딩")
        cl={"relevant":[],"dominant":[],"confidence":"NOISE","publishable":False,"leadTitle":"","share":0,"embeddingUsed":False} if is_noise_topic(topic) else cluster(conn,topic,arts)
        candidate_rows.append({"rawRank":raw_rank,"topic":topic,"suddenlyWhyScore":x["suddenlyWhyScore"],"confidence":cl["confidence"],"publishable":cl["publishable"],"newsSearchBreakdown":sort_counts,"relevantArticleCount":len(cl["relevant"]),"dominantClusterCount":len(cl["dominant"]),"dominantClusterShare":cl["share"],"leadArticleTitle":cl["leadTitle"],"clusterArticleTitles":[clean_headline(a["title"]) for a in cl["dominant"]],"embeddingUsed":cl["embeddingUsed"]})
        analysis_fp=evidence_fingerprint(topic,list(cl.get("dominant") or arts))
        previous=latest_reusable_item(conn,topic,analysis_fp,run_now)
        if previous:
            display_rank=len(final)+1
            reused_item=dict(previous)
            reused_item.update({"score":x["suddenlyWhyScore"],"analysisFingerprint":analysis_fp,"reusedAnalysis":True})
            final.append(add_site_fields(conn,reused_item,raw_rank,display_rank))
            reused+=1
            log(f"    -> SITE #{display_rank} 이전 최종 검증 결과 재사용 (GPT 호출 0회)")
            continue
        log("    3) GPT 근거 검증·카드 문장 생성")
        try:
            normal=validate_normal(conn,x,cl,run_now)
        except CreditBalanceExhausted:
            raise
        except Exception as e:
            normal={"rank":raw_rank,"topic":topic,"score":x["suddenlyWhyScore"],"ruleConfidence":cl["confidence"],"evidenceWindowHours":0,"aiSupported":False,"publishable":False,"cacheHit":False,"supportingSourceCount":0,"independentPublisherCount":0,"question":f"왜 '{topic}' 검색이 갑자기 늘었을까?","whyNow":"AI 검증 단계에서 오류가 발생했습니다.","summary":"원인 설명을 게시하지 않습니다.","reasonIfUnsupported":str(e),"sources":[]}

        result=normal; last_rescue=None
        if not normal.get("publishable"):
            log("    4) Broad Rescue 확장 검증")
            result,res=broad_rescue(conn,x,normal,run_now,arts,max(EVIDENCE_HOUR_WINDOWS))
            last_rescue=res
            res["rawRank"]=raw_rank
            broad.append(res)
            if result.get("publishable") and result.get("ruleConfidence")=="BROAD-RESCUE":
                rescued+=1

        if result.get("publishable"):
            result["analysisFingerprint"]=analysis_fp
            result["reusedAnalysis"]=False
            try:
                result,approved=final_verify(conn,result)
            except CreditBalanceExhausted:
                raise
            except Exception as e:
                approved=False
                result["publishable"]=False
                result["reasonIfUnsupported"]=f"최종 검증 오류: {e}"
            if not approved:
                final_rejected+=1
                rejected.append({"rawRank":raw_rank,"topic":topic,"stage":"final-verify","reason":result.get("reasonIfUnsupported")})
                log("    -> 최종 근거 검증에서 제외, 다음 원시 후보로")
                continue
            display_rank=len(final)+1
            final_item=add_site_fields(conn,result,raw_rank,display_rank)
            final.append(final_item)
            log(f"    -> SITE #{display_rank} 채택 ({result.get('ruleConfidence')})")
        else:
            rejected.append({"rawRank":raw_rank,"topic":topic,"stage":"after-broad","reason":(last_rescue or {}).get("reasonIfUnsupported") or result.get("reasonIfUnsupported") or result.get("whyNow")})
            log("    -> 설명 근거 부족, 다음 원시 후보로")
        time.sleep(0.04)

    json_dump(NEWS_PATH,{"generatedAt":run_now.strftime("%Y-%m-%d %H:%M:%S"),"apiAvailable":True,"note":"Only candidates actually analyzed before TOP10 was filled","evidence":evidence_rows})
    json_dump(CANDIDATE_PATH,{"generatedAt":run_now.strftime("%Y-%m-%d %H:%M:%S"),"version":VERSION,"note":"Candidates analyzed until 10 publishable topics were filled","results":candidate_rows})
    json_dump(BROAD_PATH,{"generatedAt":run_now.strftime("%Y-%m-%d %H:%M:%S"),"version":VERSION,"note":"Rejected candidates only; useful planner queries are searched even when rescueCandidate=false","results":broad})

    if len(final) < MIN_PUBLISHABLE_TO_REPLACE:
        candidate_count=len(final)
        restored=restore_last_good_if_needed(MIN_PUBLISHABLE_TO_REPLACE, force=True)
        if restored:
            api_usage=usage_summary()
            conn.close()
            write_status(
                "degraded",
                f"이번 분석 결과가 {candidate_count}개뿐이라 마지막 정상 TOP10을 유지합니다.",
                run_now,
                finalPreserved=True,
                rejectedCandidateCount=candidate_count,
                minimumPublishable=MIN_PUBLISHABLE_TO_REPLACE,
                apiUsage=api_usage,
            )
            log(f"\n[보호] 게시 가능한 결과가 {candidate_count}개뿐이라 기존 정상 TOP10을 복원했습니다.")
            log(f"Responses API: {api_usage['responseRequests']}회")
            return 0
        if not final:
            raise RuntimeError("게시 가능한 결과가 0개라 마지막 정상 TOP10을 보존합니다.")
    api_usage=usage_summary()
    final_payload={"generatedAt":run_now.strftime("%Y-%m-%d %H:%M:%S"),"version":VERSION,"model":FAST_MODEL,"verifyModel":VERIFY_MODEL,"embeddingModel":EMBEDDING_MODEL if USE_EMBEDDINGS else None,"note":"Free hourly signals are separated from changed-topic AI analysis and final evidence verification.","stats":{"poolHours":pool_hours,"rawCandidates":len(raw),"analyzedCandidates":analyzed,"publishableCount":len(final),"reusedAnalyses":reused,"finalVerifyRejected":final_rejected,"broadAttempted":len([b for b in broad if b.get('attempted')]),"broadRescued":rescued,"genericFastRejected":generic_skipped,"embeddingEnabled":USE_EMBEDDINGS},"apiUsage":api_usage,"rankings":final,"rejected":rejected}
    json_dump(FINAL_PATH,final_payload)
    json_dump(TOP_PATH,final_payload)
    json_dump(LAST_GOOD_PATH,final_payload)

    log("\n[3/8] SQLite 랭킹 snapshot 저장")
    rid=save_history(conn,run_now,final)
    log("[4/8] 24시간 집계 JSON")
    export_history(conn,run_now,24,HISTORY_24H_PATH)
    log("[5/8] 7일 집계 JSON")
    export_history(conn,run_now,168,HISTORY_7D_PATH)
    log("[6/8] 30일 집계 JSON")
    export_history(conn,run_now,720,HISTORY_30D_PATH)
    write_status("ok","새 TOP10 분석을 정상 완료했습니다.",run_now,last_ok=final_payload["generatedAt"],
                 fastModel=FAST_MODEL,verifyModel=VERIFY_MODEL,publishableCount=len(final),reusedAnalyses=reused,
                 apiUsage=api_usage)
    log("[7/8] 웹 API용 데이터 준비 완료")
    log("[8/8] 완료")
    conn.close()

    log("\n"+"="*64); log(f"사이트 TOP10: {len(final)}개 / 원시 후보 {analyzed}개 분석")
    for x in final:
        log(f"  SITE #{x['rank']} (RAW #{x['rawRank']}) {x.get('displayTopic') or x['topic']} - {x.get('whyNow','')[:55]}")
    if len(final)<TOP_COUNT:
        log(f"\n[주의] 최근 {pool_hours}시간 후보 {len(raw)}개를 모두 검증했지만 설명 가능한 항목이 {len(final)}개뿐입니다.")
    log(f"\nResponses API: {api_usage['responseRequests']}회")
    for model_name,model_usage in api_usage["models"].items():
        log(f"  {model_name}: {model_usage['requests']}회 / 입력 {model_usage['inputTokens']:,} / 출력 {model_usage['outputTokens']:,} tokens")
    if api_usage.get("estimatedResponseCostUsd") is not None:
        log(f"  텍스트 응답 예상 비용: 약 ${api_usage['estimatedResponseCostUsd']:.4f} (임베딩/세금 제외)")
    log(f"\nFINAL : {FINAL_PATH}\nRAW   : {RAW_PATH}\nDB    : {DB_PATH}\n24h   : {HISTORY_24H_PATH}\n7d    : {HISTORY_7D_PATH}\n30d   : {HISTORY_30D_PATH}\nrun_id: {rid}")
    return 0


if __name__=="__main__":
    try: raise SystemExit(main())
    except KeyboardInterrupt: raise SystemExit(130)
    except CreditBalanceExhausted:
        handle_quota_failure(now_local())
        raise SystemExit(2)
    except Exception as e:
        restore_last_good_if_needed()
        write_status("error",f"새 분석 중 오류가 발생했습니다: {e}",now_local(),
                     finalPreserved=usable_final(json_load(FINAL_PATH, {})),apiUsage=usage_summary())
        print("\n실행 실패\n"+str(e)); print("\n마지막 정상 TOP10은 보존됩니다."); print("환경변수: NAVER_API_HUB_CLIENT_ID / NAVER_API_HUB_CLIENT_SECRET / OPENAI_API_KEY"); raise SystemExit(1)

