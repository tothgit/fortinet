#!/usr/bin/env python3
"""
Fortinet Product Lifecycle Radar — daily data refresh.

Downloads the three official Fortinet lifecycle RSS feeds in full (never
truncated/summarized), recomputes each item's status, and renders the
result into productlifecycle/index.html from productlifecycle/template.html.

Run by .github/workflows/daily-update.yml on a daily schedule.
"""
import datetime as dt
import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python <3.9 fallback
    ZoneInfo = None

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "productlifecycle"
TEMPLATE_PATH = OUT_DIR / "template.html"
INDEX_PATH = OUT_DIR / "index.html"

FEEDS = {
    "hardware": "https://support.fortinet.com/rss/Hardware.xml",
    "software": "https://support.fortinet.com/rss/Software.xml",
    "services": "https://support.fortinet.com/rss/Services.xml",
}

SOON_DAYS = 180
MID_DAYS = 365


def today_prague():
    if ZoneInfo is not None:
        return dt.datetime.now(ZoneInfo("Europe/Prague")).date()
    return date.today()


TODAY = today_prague()


def download(url, dest):
    req = urllib.request.Request(url, headers={"User-Agent": "fortinet-lifecycle-radar/1.0"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    dest.write_bytes(data)
    return data


def parse_feed(raw_bytes):
    root = ET.fromstring(raw_bytes)
    channel = root.find("channel")
    if channel is None:
        raise ValueError("RSS feed missing <channel> element")
    items = []
    for item in channel.findall("item"):
        title = (item.findtext("title", default="") or "").strip()
        desc = (item.findtext("description", default="") or "").strip()
        pubdate_raw = (item.findtext("pubDate", default="") or "").strip()
        parsed_dt = parsedate_to_datetime(pubdate_raw) if pubdate_raw else None
        fields = {}
        for part in desc.split(", "):
            part = part.replace("<br/>", "").strip()
            if ":" in part:
                k, v = part.split(":", 1)
                fields[k.strip()] = v.strip()
        items.append({"title": title, "pubdate": parsed_dt.date() if parsed_dt else None, "fields": fields})
    return items


def status_for(eos_str):
    if not eos_str or eos_str in ("N/A", "—", "-"):
        return "unknown"
    try:
        eos = date.fromisoformat(eos_str)
    except ValueError:
        return "unknown"
    delta = (eos - TODAY).days
    if delta < 0:
        return "past"
    if delta <= SOON_DAYS:
        return "soon"
    if delta <= MID_DAYS:
        return "mid"
    return "ok"


def family_from_title(title):
    return re.sub(r"\s+[\d.]+$", "", title).strip()


def build_hw_svc(items):
    total_feed = len(items)
    rows = []
    for it in items:
        if not it["pubdate"]:
            continue
        f = it["fields"]
        eos = f.get("End of Support", "")
        rows.append({
            "t": it["title"],
            "c": f.get("Category", ""),
            "p": it["pubdate"].isoformat(),
            "eo": f.get("End of Order", ""),
            "lse": f.get("Last Service Extension", ""),
            "eos": eos,
            "s": status_for(eos),
        })
    rows.sort(key=lambda r: r["p"])
    return {"rows": rows, "total_feed": total_feed, "count": len(rows)}


def build_sw(items):
    total_feed = len(items)
    rows = []
    for it in items:
        if not it["pubdate"]:
            continue
        f = it["fields"]
        eos = f.get("End of Support", "")
        rows.append({
            "t": it["title"],
            "c": family_from_title(it["title"]),
            "p": it["pubdate"].isoformat(),
            "rel": f.get("Release", ""),
            "eng": f.get("End of Engineering Support", ""),
            "eos": eos,
            "s": status_for(eos),
        })
    rows.sort(key=lambda r: r["p"])
    return {"rows": rows, "total_feed": total_feed, "count": len(rows)}


def main():
    tmp_dir = REPO_ROOT / ".github" / "scripts" / "_rss_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    raw = {}
    for name, url in FEEDS.items():
        dest = tmp_dir / f"{name}.xml"
        print(f"Downloading {url} ...", file=sys.stderr)
        data = download(url, dest)
        if not data or len(data) < 200:
            print(f"ERROR: {name}.xml looks empty/too small ({len(data)} bytes) — aborting, not touching index.html", file=sys.stderr)
            sys.exit(1)
        raw[name] = data

    parsed = {name: parse_feed(data) for name, data in raw.items()}

    data_out = {
        "hardware": build_hw_svc(parsed["hardware"]),
        "software": build_sw(parsed["software"]),
        "services": build_hw_svc(parsed["services"]),
    }

    for cat, d in data_out.items():
        print(f"{cat}: total_feed={d['total_feed']} count={d['count']}", file=sys.stderr)
        if d["count"] == 0:
            print(f"ERROR: {cat} has 0 rows — feed format may have changed, aborting", file=sys.stderr)
            sys.exit(1)

    if not TEMPLATE_PATH.exists():
        print(f"ERROR: template not found at {TEMPLATE_PATH}", file=sys.stderr)
        sys.exit(1)

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(data_out, ensure_ascii=False, separators=(", ", ": "))

    rendered = template.replace("__TODAY_ISO__", TODAY.isoformat())
    rendered = rendered.replace("__DATA_JSON__", data_json)

    if rendered == template:
        print("ERROR: substitution had no effect — placeholders not found in template", file=sys.stderr)
        sys.exit(1)

    INDEX_PATH.write_text(rendered, encoding="utf-8")
    print(f"Wrote {INDEX_PATH} ({len(rendered)} bytes), TODAY={TODAY.isoformat()}", file=sys.stderr)


if __name__ == "__main__":
    main()
