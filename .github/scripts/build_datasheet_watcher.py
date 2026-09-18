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
import time
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
# Lowered from 5: fortinet.com's CDN/WAF appears to actively drop some
# connections from GitHub Actions' shared runner IP ranges (confirmed live
# on 2026-09-18 — "Remote end closed connection without response" / SSL
# EOF errors on PDFs that respond normally from other networks). A lower,
# less bursty concurrency plus HEAD_RETRIES below is a mitigation, not a
# guaranteed fix — this traffic-shaping risk was already flagged as
# possible when the /data-sheets/pdf/ sub-path was first tracked.
HEAD_MAX_WORKERS = 2
HEAD_RETRIES = 2  # extra attempts after the first, on connection-level errors only
HEAD_RETRY_DELAY = 2.0  # seconds, fixed backoff between attempts

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
    """Return (headers, final_url) for a URL, following redirects normally.

    Redirects ARE followed here (unlike an earlier version of this script)
    because several tracked links 301-redirect to a *different but still
    real* PDF (e.g. a renamed/superseded model) — a browser follows that
    automatically and the person gets a working file. What still needs
    catching is a redirect (or even a direct 200) that does NOT end on a
    PDF at all (e.g. a dead link bounced to a generic /resources page) —
    head_check() below checks the final response's Content-Type for that,
    rather than refusing to follow the redirect in the first place.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method=method)
    with urllib.request.urlopen(req, timeout=HEAD_TIMEOUT) as resp:
        return dict(resp.headers), resp.geturl()


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
    """Return {"last_modified", "etag", "broken", "redirect_to"} for a PDF URL.

    Redirects are followed (see fetch_headers). "broken" is True only when
    the response we actually land on isn't a PDF at all — caught via its
    Content-Type — whether that happened directly or after a redirect. A
    redirect that lands on a *different* real PDF (e.g. a renamed model)
    is NOT broken: a browser follows it too, so the person gets a working
    file, just not under the originally listed name.
    """
    headers = None
    final_url = url
    last_error = None
    for attempt in range(HEAD_RETRIES + 1):
        try:
            headers, final_url = fetch_headers(url, method="HEAD")
            last_error = None
            break
        except urllib.error.HTTPError as e:
            # A real HTTP response (even an error one) — nothing to retry.
            headers = dict(e.headers) if e.headers else {}
            final_url = url
            last_error = None
            break
        except Exception as e:
            # Connection-level failure (reset, SSL EOF, timeout) — these
            # are the ones seen from GitHub Actions' shared IP ranges and
            # are worth one or two retries before giving up.
            last_error = e
            if attempt < HEAD_RETRIES:
                time.sleep(HEAD_RETRY_DELAY)
    if last_error is not None:
        log(f"  HEAD failed for {url} after {HEAD_RETRIES + 1} attempts: {last_error}")
        return {"last_modified": None, "etag": None, "broken": False, "redirect_to": None}

    content_type = (headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type and content_type != "application/pdf":
        log(f"  BROKEN LINK: {url} -> final {final_url} (content-type: {content_type or 'unknown'})")
        redirect_to = final_url if final_url != url else None
        return {"last_modified": None, "etag": None, "broken": True, "redirect_to": redirect_to}

    lm_raw = headers.get("Last-Modified") if headers else None
    etag = headers.get("ETag") if headers else None
    last_modified = None
    if lm_raw:
        try:
            last_modified = parsedate_to_datetime(lm_raw).date().isoformat()
        except Exception:
            last_modified = None
    return {"last_modified": last_modified, "etag": etag, "broken": False, "redirect_to": None}


def check_all(urls):
    results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=HEAD_MAX_WORKERS) as pool:
        future_to_url = {pool.submit(head_check, url): url for url in urls}
        for fut in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[fut]
            results[url] = fut.result()
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
    broken_count = 0

    for url, meta in listing.items():
        chk = checks.get(url, {"last_modified": None, "etag": None, "broken": False, "redirect_to": None})
        old = old_state.get(url)
        status = None  # "broken"/"unknown" set it directly below; otherwise
        # it's decided after the branch, by the rolling-window rule.

        if chk.get("broken"):
            # The URL 3xx-redirects instead of serving the PDF — the link
            # published on fortinet.com/resources/data-sheets doesn't work
            # as-is. Keep whatever last-known-good data we have (if any)
            # for display, but the status makes clear this needs attention.
            broken_count += 1
            status = "broken"
            if old:
                verified = old.get("last_modified")
                changed_on = old.get("last_changed")
                first_seen = old.get("first_seen", today_iso)
                prev = old.get("prev_last_modified")
                new_state[url] = {**old, "last_checked": today_iso}
            else:
                verified = None
                changed_on = None
                first_seen = today_iso
                prev = None
                new_state[url] = {
                    "last_modified": None, "etag": None,
                    "first_seen": today_iso, "last_checked": today_iso,
                    "last_changed": None, "prev_last_modified": None,
                }
        elif chk["last_modified"] is None and chk["etag"] is None:
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
            # status decided below by the rolling-window rule, same as the
            # "existing item" branch — see the comment above that cascade.
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
                verified = chk["last_modified"]
                changed_on = today_iso
                prev = old.get("last_modified")
            else:
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

        if status is None:
            # Rolling-window classification: an item counts as "new" for
            # RECENT_DAYS after it was first seen, and as "changed" for
            # RECENT_DAYS after its most recent *genuine* content change
            # (prev_last_modified set — i.e. not just the initial
            # discovery). Previously this only looked at today's literal
            # check, so "Změněno"/"Nově zjištěno" silently reverted to
            # "Beze změny" the very next day even for a change from
            # yesterday — this makes the "(N dní)" label on the page true.
            first_seen_days = None
            if first_seen:
                try:
                    first_seen_days = (TODAY - date.fromisoformat(first_seen)).days
                except Exception:
                    first_seen_days = None
            if prev is not None and days_ago is not None and days_ago <= RECENT_DAYS:
                status = "changed"
            elif first_seen_days is not None and first_seen_days <= RECENT_DAYS:
                status = "new"
            else:
                status = "unchanged"

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
    return rows, new_state, unknown_count, broken_count, len(removed)


def main():
    listing = discover_listing()
    old_state = load_state()

    log(f"Checking {len(listing)} PDFs via HTTP HEAD (Last-Modified/ETag, {HEAD_MAX_WORKERS} at a time)...")
    checks = check_all(list(listing.keys()))

    rows, new_state, unknown_count, broken_count, removed_count = build_rows(listing, checks, old_state)

    today_iso = TODAY.isoformat()
    # "s" is already the rolling-window status (see build_rows) — new_count
    # / changed_count below mean "within the last RECENT_DAYS days", which
    # is what the "(N dní)" labels on the page show.
    new_count = sum(1 for r in rows if r["s"] == "new")
    changed_count = sum(1 for r in rows if r["s"] == "changed")
    new_today = sum(1 for r in rows if r["fs"] == today_iso)
    changed_today = sum(1 for r in rows if r["chg"] == today_iso and r["prev"] is not None)
    restricted_total = sum(1 for r in rows if r["r"])

    log(f"Result: {len(rows)} tracked ({restricted_total} under restricted /pdf/ path), "
        f"{new_today} new today ({new_count} in last {RECENT_DAYS}d), "
        f"{changed_today} changed today ({changed_count} in last {RECENT_DAYS}d), "
        f"{unknown_count} unknown (HEAD failed), {broken_count} broken (redirected away from the PDF), "
        f"{removed_count} removed from listing")

    if not TEMPLATE_PATH.exists():
        log(f"ERROR: template not found at {TEMPLATE_PATH}")
        sys.exit(1)

    data_out = {
        "rows": rows,
        "total": len(rows),
        "new_count": new_count,
        "changed_count": changed_count,
        "unknown_count": unknown_count,
        "broken_count": broken_count,
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
