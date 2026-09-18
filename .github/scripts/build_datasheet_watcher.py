#!/usr/bin/env python3
"""
Fortinet Datasheet Watcher — daily data refresh.

Why this exists: the "Last updated" date shown on
https://www.fortinet.com/resources/data-sheets is metadata typed into the
CMS card and can be stale — verified live on 2026-09-18, the card for
"FortiGate 700G Series Datasheet" claimed "Last updated: 05/07/2025" while
the PDF's actual HTTP Last-Modified header was 2026-08-19. So this script
never trusts that page date for change detection. It only trusts the
HTTP Last-Modified / ETag response headers fetched directly from each PDF
URL, and uses the page date purely as a secondary, clearly-labelled
reference value.

Discovery: https://www.fortinet.com/resources/data-sheets?limit=200 returns
a static (non-JS-rendered) HTML page listing all data sheets in one shot —
no headless browser needed. Each entry is a `<div class="list-item">` with
the PDF link, a description and the (untrusted) page date.

robots.txt on www.fortinet.com disallows unnamed/automated user agents from
the entire site (a trailing "User-agent: * / Disallow: /" block), while
specifically named crawlers (Googlebot, Bingbot, GPTBot, ...) are allowed
everywhere except a short list of paths — one of which is
/content/dam/fortinet/assets/data-sheets/pdf/*. This script identifies
itself honestly (never spoofs an allowed bot's User-Agent). The tool's
owner decided, knowingly, to track PDFs under that sub-path too (several
current-generation FortiGate models only publish their data sheet there),
so every tracked item carries a "restricted" flag noting whether it sits
under that specific disallowed path, for transparency. Request volume is
kept low regardless (one page fetch + one HEAD per tracked PDF, once a day,
with limited concurrency).

Run by .github/workflows/datasheet-watcher-daily.yml on a daily schedule.
"""
import concurrent.futures
import datetime as dt
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import date
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover — Python <3.9 fallback
    ZoneInfo = None

REPO_ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = REPO_ROOT / "datasheet-watcher"
TEMPLATE_PATH = OUT_DIR / "template.html"
INDEX_PATH = OUT_DIR / "index.html"
STATE_PATH = OUT_DIR / "data" / "state.json"

LISTING_URL = "https://www.fortinet.com/resources/data-sheets?limit=200"
USER_AGENT = "fortinet-datasheet-watcher/1.0 (+https://github.com/tothgit/fortinet)"

# This sub-path is disallowed by robots.txt even for named/allowed
# crawlers. We still track it (decided with the tool's owner — several
# current FortiGate series only publish here), but every item under it is
# tagged "restricted" so the page can show which ones these are.
RESTRICTED_PATH_FRAGMENT = "/content/dam/fortinet/assets/data-sheets/pdf/"

RECENT_DAYS = 30
HEAD_TIMEOUT = 20
HEAD_MAX_WORKERS = 5

LIST_ITEM_RE = re.compile(
    r'<a\s+target="_blank"\s+href="(?P<href>/content/dam/fortinet/assets/data-sheets/[^"]+?\.pdf)"\s*>'
    r'\s*(?P<title>[^<]+?)\s*</a>'
    r'.*?<div class="asset-description">(?P<desc>.*?)</div>'
    r'(?:\s*<div class="asset-date">\s*Last updated:\s*(?P<date>[0-9/]+)\s*</div>)?',
    re.S,
)


def today_prague():
    if ZoneInfo is not None:
        return dt.datetime.now(ZoneInfo("Europe/Prague")).date()
    return date.today()


TODAY = today_prague()


def log(msg):
    print(msg, file=sys.stderr)


def fetch_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return resp.read()


def fetch_headers(url, method="HEAD"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)
    with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as resp:
        return dict(resp.headers)


def family_from_title(title):
    m = re.match(r"^(Forti[A-Za-z]+)", title)
    if m:
        return m.group(1)
    words = title.split()
    return words[0] if words else "Other"


def page_date_to_iso(date_str):
    if not date_str:
        return None
    try:
        m, d, y = date_str.strip().split("/")
        return date(int(y), int(m), int(d)).isoformat()
    except Exception:
        return None


def discover_listing():
    log(f"Downloading {LISTING_URL} ...")
    raw = fetch_bytes(LISTING_URL)
    html = raw.decode("utf-8", errors="replace")
    if len(html) < 5000:
        raise RuntimeError(f"Listing page looks too small ({len(html)} chars) — aborting")

    items = {}
    restricted_count = 0
    for m in LIST_ITEM_RE.finditer(html):
        href = m.group("href")
        restricted = RESTRICTED_PATH_FRAGMENT in href
        title = unescape(m.group("title")).strip()
        desc = unescape(re.sub(r"<[^>]+>", "", m.group("desc"))).strip()
        page_date = page_date_to_iso(m.group("date"))
        url = "https://www.fortinet.com" + href
        if url in items:
            continue  # dedupe
        if restricted:
            restricted_count += 1
        items[url] = {
            "title": title,
            "family": family_from_title(title),
            "desc": desc,
            "page_date": page_date,
            "restricted": restricted,
        }

    log(f"Listing parsed: {len(items)} PDFs kept ({restricted_count} under the robots.txt-restricted /data-sheets/pdf/ path)")
    if len(items) < 50:
        raise RuntimeError(f"Only {len(items)} data sheets found — page format may have changed, aborting")
    return items


def head_check(url):
    """Return (last_modified_iso, etag) for a PDF URL, or (None, None) on failure."""
    try:
        headers = fetch_headers(url, method="HEAD")
    except urllib.error.HTTPError as e:
        headers = dict(e.headers) if e.headers else {}
    except Exception as e:
        log(f"  HEAD failed for {url}: {e}")
        return None, None

    lm_raw = headers.get("Last-Modified") if headers else None
    etag = headers.get("ETag") if headers else None
    last_modified = None
    if lm_raw:
        try:
            last_modified = parsedate_to_datetime(lm_raw).date().isoformat()
        except Exception:
            last_modified = None
    return last_modified, etag


def check_all(urls):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=HEAD_MAX_WORKERS) as pool:
        future_to_url = {pool.submit(head_check, url): url for url in urls}
        for fut in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[fut]
            last_modified, etag = fut.result()
            results[url] = {"last_modified": last_modified, "etag": etag}
    return results


def load_state():
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            log(f"WARNING: could not parse existing state.json ({e}) — starting fresh")
    return {}


def build_rows(listing, checks, old_state):
    rows = []
    new_state = {}
    today_iso = TODAY.isoformat()
    unknown_count = 0

    for url, meta in listing.items():
        chk = checks.get(url, {"last_modified": None, "etag": None})
        old = old_state.get(url)

        if chk["last_modified"] is None and chk["etag"] is None:
            # HEAD failed — carry forward whatever we knew before.
            unknown_count += 1
            if old:
                new_state[url] = old
                status = "unknown"
                verified = old.get("last_modified")
                changed_on = old.get("last_changed")
                first_seen = old.get("first_seen", today_iso)
                prev = old.get("prev_last_modified")
            else:
                new_state[url] = {
                    "last_modified": None, "etag": None,
                    "first_seen": today_iso, "last_checked": today_iso,
                    "last_changed": None, "prev_last_modified": None,
                }
                status = "unknown"
                verified = None
                changed_on = None
                first_seen = today_iso
                prev = None
        elif old is None:
            status = "new"
            verified = chk["last_modified"]
            changed_on = today_iso
            first_seen = today_iso
            prev = None
            new_state[url] = {
                "last_modified": chk["last_modified"], "etag": chk["etag"],
                "first_seen": first_seen, "last_checked": today_iso,
                "last_changed": changed_on, "prev_last_modified": None,
            }
        else:
            changed = (
                (chk["etag"] and old.get("etag") and chk["etag"] != old.get("etag"))
                or (chk["last_modified"] and old.get("last_modified") and chk["last_modified"] != old.get("last_modified"))
            )
            if changed:
                status = "changed"
                verified = chk["last_modified"]
                changed_on = today_iso
                prev = old.get("last_modified")
            else:
                status = "unchanged"
                verified = chk["last_modified"] or old.get("last_modified")
                changed_on = old.get("last_changed")
                prev = old.get("prev_last_modified")
            first_seen = old.get("first_seen", today_iso)
            new_state[url] = {
                "last_modified": chk["last_modified"] or old.get("last_modified"),
                "etag": chk["etag"] or old.get("etag"),
                "first_seen": first_seen, "last_checked": today_iso,
                "last_changed": changed_on, "prev_last_modified": prev,
            }

        days_ago = None
        if changed_on:
            try:
                days_ago = (TODAY - date.fromisoformat(changed_on)).days
            except Exception:
                days_ago = None

        rows.append({
            "t": meta["title"],
            "f": meta["family"],
            "u": url,
            "pd": meta["page_date"],
            "v": verified,
            "prev": prev,
            "s": status,
            "chg": changed_on,
            "days": days_ago,
            "fs": first_seen,
            "r": meta["restricted"],
        })

    removed = [u for u in old_state if u not in listing]

    rows.sort(key=lambda r: (r["chg"] or "0000-00-00"), reverse=True)
    return rows, new_state, unknown_count, len(removed)


def main():
    listing = discover_listing()
    old_state = load_state()

    log(f"Checking {len(listing)} PDFs via HTTP HEAD (Last-Modified/ETag, {HEAD_MAX_WORKERS} at a time)...")
    checks = check_all(list(listing.keys()))

    rows, new_state, unknown_count, removed_count = build_rows(listing, checks, old_state)

    new_count = sum(1 for r in rows if r["s"] == "new")
    changed_count = sum(1 for r in rows if r["s"] == "changed")
    recent_count = sum(1 for r in rows if r["days"] is not None and r["days"] <= RECENT_DAYS and r["s"] in ("new", "changed"))
    restricted_total = sum(1 for r in rows if r["r"])

    log(f"Result: {len(rows)} tracked ({restricted_total} under restricted /pdf/ path), {new_count} new, {changed_count} changed today, "
        f"{recent_count} changed in last {RECENT_DAYS}d, {unknown_count} unknown (HEAD failed), {removed_count} removed from listing")

    if not TEMPLATE_PATH.exists():
        log(f"ERROR: template not found at {TEMPLATE_PATH}")
        sys.exit(1)

    data_out = {
        "rows": rows,
        "total": len(rows),
        "new_count": new_count,
        "changed_count": changed_count,
        "recent_count": recent_count,
        "unknown_count": unknown_count,
        "removed_count": removed_count,
        "recent_days": RECENT_DAYS,
        "restricted_total": restricted_total,
    }

    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    data_json = json.dumps(data_out, ensure_ascii=False, separators=(",", ":"))
    rendered = template.replace("__TODAY_ISO__", TODAY.isoformat())
    rendered = rendered.replace("__DATA_JSON__", data_json)

    if rendered == template:
        log("ERROR: substitution had no effect — placeholders not found in template")
        sys.exit(1)

    INDEX_PATH.write_text(rendered, encoding="utf-8")
    log(f"Wrote {INDEX_PATH} ({len(rendered)} bytes)")

    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    log(f"Wrote {STATE_PATH} ({len(new_state)} tracked URLs)")


if __name__ == "__main__":
    main()
