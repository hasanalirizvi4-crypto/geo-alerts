#!/usr/bin/env python3
"""Free geopolitical early-warning bot.

Sources : public RSS feeds + GDELT + Forex Factory calendar feed
Scoring : Google Gemini free tier (optional; falls back to rules if unavailable)
Delivery: Telegram bot
State   : state.json (dedupe between runs)
"""
import calendar
import hashlib
import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

UTC = timezone.utc
PKT = ZoneInfo("Asia/Karachi")
UA = {"User-Agent": "Mozilla/5.0 (geo-alerts personal bot)"}
STATE_FILE = "state.json"

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TG_CHAT = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_KEY = os.environ.get("GEMINI_API_KEY", "")
# Free-tier model names change over time; override via the GEMINI_MODEL env var if needed.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
MIN_LEVEL = os.environ.get("MIN_LEVEL", "HIGH").upper()
MAX_AGE_H = float(os.environ.get("MAX_AGE_HOURS", "4"))
MAX_LLM_CLUSTERS = int(os.environ.get("MAX_LLM_CLUSTERS", "10"))
FF_CURRENCIES = {c.strip().upper() for c in os.environ.get("FF_CURRENCIES", "USD").split(",") if c.strip()}
FF_LEAD_MIN = int(os.environ.get("FF_LEAD_MINUTES", "60"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

LEVELS = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]

FEEDS = {
    "BBC World": "https://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Asia": "https://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "BBC Middle East": "https://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
    "BBC Europe": "https://feeds.bbci.co.uk/news/world/europe/rss.xml",
    "Al Jazeera": "https://www.aljazeera.com/xml/rss/all.xml",
    "Dawn": "https://www.dawn.com/feeds/home",
    "Guardian World": "https://www.theguardian.com/world/rss",
    "NPR World": "https://feeds.npr.org/1004/rss.xml",
    "DW": "https://rss.dw.com/rdf/rss-en-all",
    "France 24": "https://www.france24.com/en/rss",
    "UN News": "https://news.un.org/feed/subscribe/en/news/all/rss.xml",
}

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

KEYWORDS = re.compile(
    r"\b(missile|airstrike|air strike|drone strike|shelling|troops|mobili[sz]ation|invasion|"
    r"ceasefire|truce|sanction|embargo|evacuat\w*|embassy|nuclear|warship|naval|blockade|"
    r"airspace|state of emergency|martial law|coup|assassinat\w*|hostage|escalat\w*|"
    r"border clash|line of control|strait|hormuz|red sea|houthi|taiwan|kashmir|"
    r"cyber ?attack|pipeline|oil price|attack)\b",
    re.I,
)

STOP = set(
    "the and for with from that this after over into amid says said news will have has had "
    "been are was were its their they them what when where which while about more than".split()
)

SYSTEM_PROMPT = """You are a geopolitical early-warning analyst. You get clusters of news headlines
(each cluster = one likely event, with the outlets that reported it). For each cluster return a JSON object.

Rules:
- Use ONLY the headlines provided. Never invent facts, quotes, numbers or sources.
- If a claim rests on one outlet or uses words like 'reportedly' or 'claims', say it is unconfirmed.
- Do not exaggerate or predict with certainty.
- level: CRITICAL = imminent/active major military escalation, nuclear or chokepoint closure, attack on a state's
  territory or capital; HIGH = significant escalation, major sanctions, mobilization, embassy evacuations, large
  attacks; MEDIUM = notable but contained; LOW = routine or minor.
- Priority regions: South Asia (Pakistan-India, China-India, Afghanistan), China-US, Taiwan Strait, Middle East,
  Russia-Ukraine, NATO-Russia, Red Sea and major shipping routes, energy chokepoints.

Return ONLY a JSON array, no markdown. One object per cluster:
{"id": <int>, "level": "LOW|MEDIUM|HIGH|CRITICAL", "event": "<one sentence>", "location": "<place>",
 "why_it_matters": "<1-2 sentences>", "watch_next": ["<indicator>", "<indicator>", "<indicator>"],
 "caveats": "<unverified claims / what is unknown, or empty string>"}
"""


def log(msg):
    print(f"[{datetime.now(UTC):%H:%M:%S}] {msg}", flush=True)


def esc(s):
    return html.escape(str(s or ""))


def toks(s):
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if len(w) > 3 and w not in STOP}


def similar(a, b):
    shared = len(a & b)
    return shared >= 2 and shared / len(a | b) >= 0.3


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            s = json.load(f)
    except Exception:
        s = {}
    s.setdefault("alerted", [])  # [{"title": str, "ts": float}]
    s.setdefault("ff", {})  # {event_id: ts}
    return s


def save_state(state):
    cutoff = time.time() - 3 * 86400
    state["alerted"] = [a for a in state["alerted"] if a["ts"] > cutoff]
    state["ff"] = {k: v for k, v in state["ff"].items() if v > cutoff}
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=1)


# ---------- ingest ----------
def fetch_rss(cutoff):
    items = []
    for name, url in FEEDS.items():
        try:
            r = requests.get(url, timeout=20, headers=UA)
            r.raise_for_status()
            feed = feedparser.parse(r.content)
            for e in feed.entries:
                title = (e.get("title") or "").strip()
                t = e.get("published_parsed") or e.get("updated_parsed")
                if not title or not t:
                    continue
                ts = datetime.fromtimestamp(calendar.timegm(t), UTC)
                blob = title + " " + (e.get("summary") or "")[:300]
                if ts < cutoff or not KEYWORDS.search(blob):
                    continue
                items.append({"title": title, "link": e.get("link", ""), "source": name, "ts": ts})
        except Exception as ex:
            log(f"feed failed: {name}: {ex}")
    return items


def fetch_gdelt(cutoff):
    query = (
        "(missile OR airstrike OR mobilization OR ceasefire OR sanctions OR evacuation OR blockade) "
        '(Pakistan OR India OR Taiwan OR Iran OR Israel OR Ukraine OR "Red Sea" OR Hormuz OR China)'
    )
    params = {
        "query": query,
        "mode": "artlist",
        "format": "json",
        "maxrecords": 75,
        "timespan": f"{int(MAX_AGE_H * 60)}min",
        "sort": "datedesc",
    }
    out = []
    try:
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc", params=params, timeout=30, headers=UA)
        data = r.json()
        for a in data.get("articles", []):
            if a.get("language", "English") != "English":
                continue
            ts = datetime.strptime(a["seendate"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
            if ts < cutoff:
                continue
            out.append({"title": a["title"], "link": a["url"], "source": a.get("domain", "gdelt"), "ts": ts})
    except Exception as ex:
        log(f"gdelt failed: {ex}")
    return out


# ---------- cluster ----------
def build_clusters(items):
    clusters = []
    for it in sorted(items, key=lambda x: x["ts"]):
        tk = toks(it["title"])
        for c in clusters:
            if similar(tk, c["tokens"]):
                c["items"].append(it)
                break
        else:
            clusters.append({"tokens": tk, "items": [it]})
    for c in clusters:
        c["sources"] = sorted({i["source"] for i in c["items"]})
        c["kw"] = sum(len(KEYWORDS.findall(i["title"])) for i in c["items"])
    return clusters


# ---------- scoring ----------
def score_with_llm(clusters):
    if not GEMINI_KEY or not clusters:
        return None
    payload = [
        {
            "id": i,
            "independent_sources": len(c["sources"]),
            "headlines": [f'{it["source"]}: {it["title"]}' for it in c["items"][:5]],
        }
        for i, c in enumerate(clusters)
    ]
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "contents": [{"parts": [{"text": SYSTEM_PROMPT + "\nCLUSTERS:\n" + json.dumps(payload, ensure_ascii=False)}]}],
        "generationConfig": {"temperature": 0.2, "responseMimeType": "application/json"},
    }
    try:
        r = requests.post(
            url, json=body, timeout=90, headers={"x-goog-api-key": GEMINI_KEY, "Content-Type": "application/json"}
        )
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
        return {int(x["id"]): x for x in json.loads(text)}
    except Exception as ex:
        log(f"LLM scoring failed (falling back to rules): {ex}")
        return None


def fallback_score(c):
    n = len(c["sources"])
    return {
        "level": "HIGH" if n >= 3 else "MEDIUM",
        "event": c["items"][0]["title"],
        "location": "",
        "why_it_matters": "Automatic alert: several outlets are reporting this. AI analysis was unavailable.",
        "watch_next": [],
        "caveats": "",
    }


# ---------- messages ----------
def fmt_alert(c, s):
    n = len(c["sources"])
    conf = "🟢 High" if n >= 3 else "🟡 Medium" if n == 2 else "🔴 Low"
    first = min(i["ts"] for i in c["items"])
    seen, links = set(), []
    for it in c["items"]:
        if it["source"] not in seen and len(links) < 4:
            seen.add(it["source"])
            links.append(f'• <a href="{esc(it["link"])}">{esc(it["source"])}</a>')
    lines = [
        f"🚨 <b>GEOPOLITICAL ALERT — {esc(s.get('level', 'HIGH'))}</b>",
        "",
        f"<b>Event:</b> {esc(s.get('event'))}",
        f"<b>First seen:</b> {first.astimezone(PKT):%d %b %Y, %H:%M} PKT ({first:%H:%M} UTC)",
        f"<b>Location:</b> {esc(s.get('location') or 'Not specified')}",
        f"<b>Confidence:</b> {conf} ({n} independent source{'s' if n != 1 else ''})",
        "",
        "<b>Sources:</b>",
        *links,
        "",
        f"<b>Why it matters:</b> {esc(s.get('why_it_matters'))}",
    ]
    watch = [w for w in (s.get("watch_next") or []) if w]
    if watch:
        lines += ["", "<b>What to watch next:</b>"] + [f"{i}. {esc(w)}" for i, w in enumerate(watch[:3], 1)]
    if s.get("caveats"):
        lines += ["", f"<b>Unverified / unknown:</b> {esc(s['caveats'])}"]
    return "\n".join(lines)[:4000]


def send(text):
    if DRY_RUN or not (TG_TOKEN and TG_CHAT):
        print("---- (not sent: DRY_RUN or Telegram not configured) ----\n" + text + "\n")
        return DRY_RUN  # dry runs count as delivered; a missing Telegram config does not
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
            json={"chat_id": TG_CHAT, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=20,
        )
        if not r.ok:
            log(f"telegram error: {r.status_code} {r.text[:200]}")
        return r.ok
    except Exception as ex:
        log(f"telegram failed: {ex}")
        return False


# ---------- Forex Factory ----------
def ff_alerts(state, now):
    try:
        r = requests.get(FF_URL, timeout=20, headers=UA)
        r.raise_for_status()
        events = r.json()
    except Exception as ex:
        log(f"forex factory feed failed: {ex}")
        return
    for e in events:
        if e.get("impact") != "High" or (e.get("country") or "").upper() not in FF_CURRENCIES:
            continue
        try:
            dt = datetime.fromisoformat(e["date"]).astimezone(UTC)
        except Exception:
            continue
        mins = (dt - now).total_seconds() / 60
        if not 0 <= mins <= FF_LEAD_MIN:
            continue
        eid = hashlib.md5(f'{e.get("title")}|{e.get("date")}|{e.get("country")}'.encode()).hexdigest()
        if eid in state["ff"]:
            continue
        msg = "\n".join(
            [
                "📅 <b>HIGH-IMPACT DATA SOON</b> (Forex Factory)",
                "",
                f"<b>{esc(e.get('country'))} — {esc(e.get('title'))}</b>",
                f"<b>Time:</b> {dt.astimezone(PKT):%a %d %b, %H:%M} PKT ({dt:%H:%M} UTC), in {int(mins)} min",
                f"<b>Forecast:</b> {esc(e.get('forecast') or '—')}   <b>Previous:</b> {esc(e.get('previous') or '—')}",
            ]
        )
        if send(msg) or DRY_RUN:
            state["ff"][eid] = time.time()


# ---------- main ----------
def main():
    state = load_state()
    now = datetime.now(UTC)
    cutoff = now - timedelta(hours=MAX_AGE_H)

    items = fetch_rss(cutoff) + fetch_gdelt(cutoff)
    log(f"{len(items)} relevant headlines")
    clusters = build_clusters(items)

    recent = [toks(a["title"]) for a in state["alerted"]]
    fresh = [c for c in clusters if not any(similar(c["tokens"], r) for r in recent)]
    fresh.sort(key=lambda c: (len(c["sources"]), c["kw"]), reverse=True)
    top = fresh[:MAX_LLM_CLUSTERS]
    log(f"{len(clusters)} clusters, {len(top)} sent for scoring")

    scores = score_with_llm(top)
    for i, c in enumerate(top):
        s = scores.get(i) if scores else None
        s = s or (fallback_score(c) if scores is None else None)
        if not s or s.get("level", "LOW") not in LEVELS:
            continue
        if LEVELS.index(s["level"]) < LEVELS.index(MIN_LEVEL):
            continue
        if send(fmt_alert(c, s)) or DRY_RUN:
            state["alerted"].append({"title": c["items"][0]["title"], "ts": time.time()})

    ff_alerts(state, now)

    if not DRY_RUN:
        save_state(state)


if __name__ == "__main__":
    main()
