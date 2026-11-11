"""Zbiera nowe filmy: subskrypcje (RSS) + wyszukiwanie tematyczne (YouTube Data API).

Wynik: data/latest.json, który co rano czyta Claude i robi z niego digest.
Klucz API w sekrecie repo: YT_API_KEY. Bez klucza działa sam RSS.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
RESOLVED = DATA / "channels_resolved.json"
SEEN = DATA / "seen.json"
LATEST = DATA / "latest.json"
DISCOVERED = DATA / "discovered_channels.json"

API_KEY = os.environ.get("YT_API_KEY", "").strip()
# prywatne zapytania rozwojowe (sekret YT_EXTRA_QUERIES, jedno na linię) - zawsze wykonywane
EXTRA_QUERIES = [l.strip() for l in os.environ.get("YT_EXTRA_QUERIES", "").splitlines()
                 if l.strip() and not l.strip().startswith("#")]
API = "https://www.googleapis.com/youtube/v3"

LOOKBACK_HOURS = 48           # okno czasowe nowych filmów
SEARCH_PER_QUERY = 15         # wyników na zapytanie (1 zapytanie = 100 jednostek limitu)
MAX_SEARCH_QUERIES = 50       # 50 x 100 = 5000 z 10 000 dziennego limitu
MIN_DURATION_S = 240          # krótsze odrzucamy (Shorts, zajawki)
MIN_DURATION_SEARCH_S = 480   # dla filmów spoza subów: min. 8 minut
MAX_SEARCH_CANDIDATES = 120   # ile filmów spoza subów przekazać do oceny
SEEN_KEEP = 4000

UA = {"User-Agent": "Mozilla/5.0 (yt-digest)"}
NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "yt": "http://www.youtube.com/xml/schemas/2015",
    "media": "http://search.yahoo.com/mrss/",
}


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def read_list(name: str) -> list[str]:
    p = ROOT / name
    if not p.exists():
        return []
    return [l.strip() for l in p.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.strip().startswith("#")]


def load_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return default


def api(endpoint: str, **params) -> dict:
    params["key"] = API_KEY
    r = requests.get(f"{API}/{endpoint}", params=params, timeout=30)
    if r.status_code != 200:
        log("API error", endpoint, r.status_code, r.text[:300])
        return {}
    return r.json()


def iso_duration(s: str) -> int:
    m = re.fullmatch(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    d, h, mi, se = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + se


# ---------- rozpoznawanie kanałów ----------

def resolve_channel(entry: str) -> dict | None:
    """Nazwa / @handle / link / UC-id -> {id, title}."""
    if re.fullmatch(r"UC[\w-]{22}", entry):
        return {"id": entry, "title": entry}
    if entry.startswith("http") or entry.startswith("@"):
        url = entry if entry.startswith("http") else f"https://www.youtube.com/{entry}"
        try:
            html = requests.get(url, headers=UA, timeout=20).text
            m = re.search(r'"(?:channelId|externalId)":"(UC[\w-]{22})"', html)
            t = re.search(r'<meta property="og:title" content="([^"]+)"', html)
            if m:
                return {"id": m.group(1), "title": t.group(1) if t else entry}
        except Exception as e:
            log("resolve url fail", entry, e)
        return None
    if API_KEY:
        res = api("search", part="snippet", q=entry, type="channel", maxResults=1)
        for it in res.get("items", []):
            return {"id": it["snippet"]["channelId"], "title": it["snippet"]["channelTitle"]}
        return None
    try:
        import yt_dlp
        url = f"https://www.youtube.com/results?search_query={quote(entry)}&sp=EgIQAg%253D%253D"
        with yt_dlp.YoutubeDL({"quiet": True, "extract_flat": True, "playlistend": 3,
                               "ignoreerrors": True}) as y:
            info = y.extract_info(url, download=False) or {}
        for e in info.get("entries") or []:
            cid = (e or {}).get("channel_id") or (e or {}).get("id") or ""
            if cid.startswith("UC"):
                return {"id": cid, "title": e.get("channel") or e.get("title") or entry}
    except Exception as e:
        log("resolve fail", entry, e)
    return None


def resolve_all(entries: list[str]) -> dict:
    cache = load_json(RESOLVED, {})
    for entry in entries:
        if cache.get(entry):
            continue
        cache[entry] = resolve_channel(entry)
        log("resolve:", entry, "->", cache[entry])
        time.sleep(0.5)
    cache = {k: v for k, v in cache.items() if k in entries}
    RESOLVED.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache


# ---------- źródła ----------

def from_rss(channel_id: str, since: datetime) -> list[dict]:
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
    try:
        r = requests.get(url, headers=UA, timeout=20)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        log("rss fail", channel_id, e)
        return []
    out = []
    for en in root.findall("atom:entry", NS):
        pub = datetime.fromisoformat(en.findtext("atom:published", "", NS).replace("Z", "+00:00"))
        if pub < since:
            continue
        link = en.find("atom:link", NS)
        out.append({
            "id": en.findtext("yt:videoId", "", NS),
            "title": en.findtext("atom:title", "", NS),
            "channel": en.findtext("atom:author/atom:name", "", NS),
            "channel_id": channel_id,
            "published": pub.isoformat(),
            "description": en.findtext("media:group/media:description", "", NS)[:2000],
            "is_short_link": link is not None and "/shorts/" in (link.get("href") or ""),
            "source": "subskrypcja",
        })
    return out


def from_api_search(query: str, since: datetime, dev: bool = False) -> list[dict]:
    lang = "pl"
    if query.startswith("[en]"):
        lang, query = "en", query[4:].strip()
    extra = {"relevanceLanguage": "pl", "regionCode": "PL"} if lang == "pl" else {"relevanceLanguage": "en"}
    res = api("search", part="snippet", q=query, type="video", order="relevance",
              publishedAfter=since.strftime("%Y-%m-%dT%H:%M:%SZ"),
              maxResults=SEARCH_PER_QUERY, safeSearch="none", **extra)
    out = []
    for it in res.get("items", []):
        sn = it["snippet"]
        if sn.get("liveBroadcastContent") in ("live", "upcoming"):
            continue
        out.append({
            "id": it["id"]["videoId"],
            "title": sn.get("title", ""),
            "channel": sn.get("channelTitle", ""),
            "channel_id": sn.get("channelId", ""),
            "published": sn.get("publishedAt"),
            "description": sn.get("description", ""),
            "source": "rozwój" if dev else "spoza subów",
            "query": None if dev else query,   # zapytań rozwojowych nie zapisujemy (repo jest publiczne)
        })
    return out


NON_LATIN = re.compile(r"[\u0400-\u04FF\u0600-\u06FF\u0900-\u0DFF\u0E00-\u0E7F\u3040-\u30FF\u3400-\u9FFF\uAC00-\uD7AF]")


def lang_ok(v: dict) -> bool:
    """Tylko polski i angielski (dla filmów spoza subów)."""
    if NON_LATIN.search(v.get("title", "")):
        return False
    lg = (v.get("language") or "").lower()
    return not lg or lg.startswith(("pl", "en"))


# ---------- wzbogacanie przez API (1 jednostka / 50 filmów) ----------

def enrich_api(videos: list[dict]) -> None:
    by_id = {v["id"]: v for v in videos}
    ids = list(by_id)
    for i in range(0, len(ids), 50):
        res = api("videos", part="snippet,contentDetails,statistics", id=",".join(ids[i:i + 50]))
        for it in res.get("items", []):
            v = by_id[it["id"]]
            sn, st, cd = it["snippet"], it.get("statistics", {}), it["contentDetails"]
            v["duration"] = iso_duration(cd.get("duration", ""))
            v["description"] = (sn.get("description") or v.get("description") or "")[:2500]
            v["tags"] = (sn.get("tags") or [])[:15]
            v["language"] = sn.get("defaultAudioLanguage") or sn.get("defaultLanguage")
            v["views"] = int(st.get("viewCount", 0))
            v["likes"] = int(st.get("likeCount", 0)) if "likeCount" in st else None
            v["comments"] = int(st.get("commentCount", 0)) if "commentCount" in st else None
            v["live"] = sn.get("liveBroadcastContent")
            v["chapters"] = re.findall(r"^\s*\(?\d{1,2}:\d{2}(?::\d{2})?\)?\s*[-–—]?\s*(.+)$",
                                       v["description"], flags=re.M)[:25]
    # wielkość kanałów
    chans = list({v["channel_id"] for v in videos if v.get("channel_id")})
    subs = {}
    for i in range(0, len(chans), 50):
        res = api("channels", part="statistics", id=",".join(chans[i:i + 50]))
        for it in res.get("items", []):
            st = it.get("statistics", {})
            subs[it["id"]] = None if st.get("hiddenSubscriberCount") else int(st.get("subscriberCount", 0))
    for v in videos:
        v["channel_subscribers"] = subs.get(v.get("channel_id"))


# ---------- main ----------

def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    seen = set(load_json(SEEN, []))
    blocked = {l.lower() for l in read_list("blocked.txt")}

    channels = resolve_all(read_list("channels.txt"))
    sub_ids = {c["id"] for c in channels.values() if c}

    cand: dict[str, dict] = {}
    for ch in channels.values():
        if ch:
            for v in from_rss(ch["id"], since):
                cand.setdefault(v["id"], v)
            time.sleep(0.2)
    n_subs = len(cand)

    n_search_raw = 0
    queries = read_list("queries.txt")
    if API_KEY and (queries or EXTRA_QUERIES):
        # zapytania rozwojowe zawsze; zwykłe rotują, żeby zmieścić się w limicie
        room = max(MAX_SEARCH_QUERIES - len(EXTRA_QUERIES), 0)
        if len(queries) > room:
            start = (now.toordinal() * max(room, 1)) % max(len(queries), 1)
            queries = (queries + queries)[start:start + room]
        plan = [(q, True) for q in EXTRA_QUERIES] + [(q, False) for q in queries]
        for q, dev in plan:
            for v in from_api_search(q, since, dev):
                n_search_raw += 1
                if v["channel_id"] in sub_ids:
                    continue
                cand.setdefault(v["id"], v)
    elif not API_KEY:
        log("Brak YT_API_KEY - pomijam wyszukiwanie spoza subów")

    fresh = [v for v in cand.values()
             if v["id"] not in seen and not v.get("is_short_link")
             and v.get("channel", "").lower() not in blocked]

    if API_KEY:
        enrich_api(fresh)

    out = []
    for v in fresh:
        if v.get("live") in ("live", "upcoming"):
            continue
        dur = v.get("duration") or 0
        min_d = MIN_DURATION_SEARCH_S if v["source"] != "subskrypcja" else MIN_DURATION_S
        if dur and dur < min_d:
            continue
        if v["source"] != "subskrypcja" and (not dur or not lang_ok(v)):
            continue
        v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
        if v.get("views") and v.get("likes") is not None:
            v["like_ratio_pct"] = round(100 * v["likes"] / max(v["views"], 1), 2)
        out.append(v)

    subs_out = [v for v in out if v["source"] == "subskrypcja"]
    search_out = sorted((v for v in out if v["source"] == "spoza subów"),
                        key=lambda v: -(v.get("views") or 0))[:MAX_SEARCH_CANDIDATES]
    dev_out = sorted((v for v in out if v["source"] == "rozwój"),
                     key=lambda v: -(v.get("views") or 0))[:50]
    out = subs_out + dev_out + search_out

    LATEST.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "api_key_present": bool(API_KEY),
        "stats": {
            "channels_resolved": len(sub_ids),
            "channels_unresolved": [k for k, c in channels.items() if not c],
            "from_subscriptions_raw": n_subs,
            "search_queries": (len(queries) + len(EXTRA_QUERIES)) if API_KEY else 0,
            "dev_queries": len(EXTRA_QUERIES),
            "dev_after_filters": sum(1 for v in out if v["source"] == "rozwój"),
            "search_results_raw": n_search_raw,
            "subscriptions_after_filters": len(subs_out),
            "outside_subs_after_filters": len(search_out),
        },
        "videos": out,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    seen.update(v["id"] for v in out)
    SEEN.write_text(json.dumps(list(seen)[-SEEN_KEEP:]), encoding="utf-8")
    log(f"OK: {len(subs_out)} z subów, {len(search_out)} spoza subów")


if __name__ == "__main__":
    main()
