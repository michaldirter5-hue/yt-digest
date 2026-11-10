"""Zbiera nowe filmy z subskrybowanych kanałów (RSS) i z wyszukiwań (yt-dlp).

Wynik: data/latest.json, który co rano czyta Claude i robi z niego digest.
Nie wymaga żadnych kluczy API.
"""
from __future__ import annotations

import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests
import yt_dlp

ROOT = Path(__file__).parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)
RESOLVED = DATA / "channels_resolved.json"
SEEN = DATA / "seen.json"
LATEST = DATA / "latest.json"

LOOKBACK_HOURS = 48          # jak daleko wstecz patrzymy w RSS
SEARCH_PER_QUERY = 12        # ile najnowszych wyników na zapytanie
MAX_CANDIDATES = 90          # górny limit kandydatów do wzbogacenia
MIN_DURATION_S = 180         # krótsze = Shorts/zajawki, odrzucamy
TRANSCRIPT_CHARS = 5000      # ile znaków transkryptu zapisujemy
SEEN_KEEP = 3000

UA = {"User-Agent": "Mozilla/5.0 (yt-digest; +https://github.com)"}
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


def ydl(extra: dict | None = None) -> yt_dlp.YoutubeDL:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "socket_timeout": 20, "ignoreerrors": True}
    opts.update(extra or {})
    return yt_dlp.YoutubeDL(opts)


# ---------- rozpoznawanie kanałów ----------

def resolve_channel(entry: str) -> dict | None:
    """Zamienia nazwę / @handle / link / UC-id na {id, title}."""
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
    # nazwa -> wyszukiwanie z filtrem "kanał"
    url = f"https://www.youtube.com/results?search_query={quote(entry)}&sp=EgIQAg%253D%253D"
    with ydl({"extract_flat": True, "playlistend": 3}) as y:
        info = y.extract_info(url, download=False) or {}
    for e in info.get("entries") or []:
        if not e:
            continue
        cid = e.get("channel_id") or e.get("id") or ""
        if not cid.startswith("UC"):
            m = re.search(r"(UC[\w-]{22})", e.get("url") or "")
            cid = m.group(1) if m else ""
        if cid.startswith("UC"):
            return {"id": cid, "title": e.get("channel") or e.get("title") or entry}
    return None


def resolve_all(entries: list[str]) -> dict:
    cache = load_json(RESOLVED, {})
    for entry in entries:
        if entry in cache and cache[entry]:
            continue
        r = resolve_channel(entry)
        log("resolve:", entry, "->", r)
        cache[entry] = r
        time.sleep(1)
    # usuń z cache to, czego już nie ma na liście
    cache = {k: v for k, v in cache.items() if k in entries}
    RESOLVED.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return cache


# ---------- źródła ----------

def rss_views(en) -> int:
    st = en.find("media:group/media:community/media:statistics", NS)
    try:
        return int(st.get("views", 0)) if st is not None else 0
    except ValueError:
        return 0


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
        vid = en.findtext("yt:videoId", "", NS)
        link = en.find("atom:link", NS)
        href = link.get("href") if link is not None else ""
        out.append({
            "id": vid,
            "title": en.findtext("atom:title", "", NS),
            "channel": en.findtext("atom:author/atom:name", "", NS),
            "channel_id": channel_id,
            "published": pub.isoformat(),
            "description": en.findtext("media:group/media:description", "", NS)[:1500],
            "views": rss_views(en),
            "is_short_link": "/shorts/" in href,
            "source": "subskrypcja",
        })
    return out


def from_search(query: str) -> list[dict]:
    out = []
    with ydl({"extract_flat": True}) as y:
        info = y.extract_info(f"ytsearchdate{SEARCH_PER_QUERY}:{query}", download=False) or {}
    for e in info.get("entries") or []:
        if not e or not e.get("id"):
            continue
        out.append({
            "id": e["id"],
            "title": e.get("title", ""),
            "channel": e.get("channel") or e.get("uploader") or "",
            "channel_id": e.get("channel_id") or "",
            "published": None,
            "description": (e.get("description") or "")[:1500],
            "duration": e.get("duration"),
            "views": e.get("view_count") or 0,
            "source": f"wyszukiwanie: {query}",
        })
    return out


# ---------- wzbogacanie ----------

def enrich(v: dict) -> dict:
    """Dokłada czas trwania, pełniejszy opis, datę. Best-effort."""
    try:
        with ydl() as y:
            info = y.extract_info(f"https://www.youtube.com/watch?v={v['id']}", download=False)
        if info:
            v["duration"] = info.get("duration") or v.get("duration")
            v["description"] = (info.get("description") or v.get("description") or "")[:2500]
            v["views"] = info.get("view_count") or v.get("views")
            v["likes"] = info.get("like_count")
            v["live_status"] = info.get("live_status")
            v["chapters"] = [c.get("title") for c in (info.get("chapters") or [])][:25]
            v["tags"] = (info.get("tags") or [])[:15]
            if not v.get("published") and info.get("timestamp"):
                v["published"] = datetime.fromtimestamp(info["timestamp"], timezone.utc).isoformat()
            elif not v.get("published") and info.get("upload_date"):
                v["published"] = datetime.strptime(info["upload_date"], "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
    except Exception as e:
        log("enrich fail", v["id"], e)
    return v


def transcript(video_id: str) -> str | None:
    try:
        from youtube_transcript_api import YouTubeTranscriptApi
        langs = ["pl", "en"]
        if hasattr(YouTubeTranscriptApi, "get_transcript"):   # stare wersje (<1.0)
            parts = [s["text"] for s in YouTubeTranscriptApi.get_transcript(video_id, languages=langs)]
        else:                                                   # wersje 1.x
            parts = [s.text for s in YouTubeTranscriptApi().fetch(video_id, languages=langs)]
        text = " ".join(p.replace("\n", " ") for p in parts)
        return text[:TRANSCRIPT_CHARS] if text else None
    except Exception as e:
        log("transcript fail", video_id, type(e).__name__)
        return None


# ---------- main ----------

def main():
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=LOOKBACK_HOURS)
    seen = set(load_json(SEEN, []))

    channels = resolve_all(read_list("channels.txt"))
    candidates: dict[str, dict] = {}

    for name, ch in channels.items():
        if not ch:
            continue
        for v in from_rss(ch["id"], since):
            candidates.setdefault(v["id"], v)
        time.sleep(0.3)
    n_subs = len(candidates)

    sub_ids = {c["id"] for c in channels.values() if c}
    for q in read_list("queries.txt"):
        try:
            for v in from_search(q):
                if v["channel_id"] in sub_ids:
                    continue  # i tak mamy przez RSS
                candidates.setdefault(v["id"], v)
        except Exception as e:
            log("search fail", q, e)
        time.sleep(1)

    fresh = [v for v in candidates.values() if v["id"] not in seen and not v.get("is_short_link")]
    # subskrypcje najpierw, potem wyszukiwania
    fresh.sort(key=lambda v: (v["source"] != "subskrypcja", -(v.get("views") or 0)))
    fresh = fresh[:MAX_CANDIDATES]

    out = []
    transcripts_ok = 0
    for v in fresh:
        enrich(v)
        if v.get("live_status") in ("is_upcoming", "is_live"):
            continue
        if v.get("duration") and v["duration"] < MIN_DURATION_S:
            continue
        if v.get("published"):
            try:
                if datetime.fromisoformat(v["published"]) < since:
                    continue
            except Exception:
                pass
        t = transcript(v["id"])
        if t:
            v["transcript"] = t
            transcripts_ok += 1
        v["url"] = f"https://www.youtube.com/watch?v={v['id']}"
        out.append(v)
        time.sleep(0.5)

    LATEST.write_text(json.dumps({
        "generated_at": now.isoformat(),
        "lookback_hours": LOOKBACK_HOURS,
        "stats": {
            "channels_resolved": sum(1 for c in channels.values() if c),
            "channels_unresolved": [k for k, c in channels.items() if not c],
            "from_subscriptions": n_subs,
            "candidates_total": len(candidates),
            "after_filters": len(out),
            "with_transcript": transcripts_ok,
        },
        "videos": out,
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    seen.update(v["id"] for v in out)
    SEEN.write_text(json.dumps(list(seen)[-SEEN_KEEP:]), encoding="utf-8")
    log(f"OK: {len(out)} filmów ({transcripts_ok} z transkryptem)")


if __name__ == "__main__":
    main()
