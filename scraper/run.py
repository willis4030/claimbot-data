#!/usr/bin/env python3
"""Claimbot settlement scraper.

  python scraper/run.py                        daily run -> settlements.json
  python scraper/run.py --test NAME_OR_URL     try one site, write nothing
"""
import argparse
import asyncio
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from urllib import robotparser
from urllib.parse import urlparse

from playwright.async_api import async_playwright

import parse as P

ROOT = Path(__file__).resolve().parent.parent
SOURCES = ROOT / "sources.json"
OUT = ROOT / "settlements.json"
CACHE = ROOT / "data" / "cache.json"
FORMS_KEY = "__forms__"  # claim-form checks live inside cache.json, keyed by claim URL
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 ClaimbotData/1.0 (+https://github.com/willis4030/claimbot-data)")
ROBOTS_AGENT = "ClaimbotData"
MAX_LISTING_PAGES = 6
DELAY = 1.5              # seconds between requests to the same site (plus jitter)
REFRESH_DAYS = 7         # re-read a settlement page after this many days
MAX_DETAILS_PER_RUN = 500
PRUNE_DAYS = 30          # forget cached pages not listed anywhere for this long
PARSE_VERSION = 6        # bump when parsing changes, so cached pages are re-read
FORM_VERSION = 3         # bump when classify_form changes, so cached form results are re-checked
FORM_RECHECK_DAYS = 14   # re-check a claim form's notice-ID field after this many days
MAX_FORM_CHECKS = 200    # new claim forms checked per run (the rest wait for the next run)
FORM_CONCURRENCY = 4     # claim forms are on different sites, so a few can load at once
MAX_ARCHIVE_CHECKS = 80  # Internet Archive lookups per run, for sites that block the scraper
ARCHIVE_DELAY = 2.0      # seconds between Internet Archive requests
ADMIN_MIN_CHECKED = 4    # an administrator needs this many checked settlements...
ADMIN_MIN_SHARE = 0.85   # ...this share of them requiring the ID, to call its unknowns 'likely'
# Listing sites never count as the "official claim site" for a settlement.
KNOWN_AGGREGATORS = {"topclassactions.com", "classaction.org", "claimdepot.com", "openclassactions.com",
                     "settlementscan.app", "classactionrebates.com", "lawfareclaims.org",
                     "allfreealerts.com", "fileyourclaim.co", "dapeer.com", "classaction.com",
                     "consumer-action.org", "joincase.com", "classactiontracker.com"}


def log(*a):
    print(*a, flush=True)


def summary(md):
    """Write to the GitHub Actions run summary page when running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(md + "\n")


_robots = {}


def robots_ok(url):
    """Follow robots.txt the way search engines do (RFC 9309).

    200: obey its rules. 4xx (missing or blocked file): no rules apply.
    5xx or unreachable: stay off that site for this run.
    """
    u = urlparse(url)
    base = f"{u.scheme}://{u.netloc}"
    if base not in _robots:
        try:
            req = urllib.request.Request(base + "/robots.txt", headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                rp = robotparser.RobotFileParser()
                rp.parse(r.read().decode("utf-8", "replace").splitlines())
            state = rp
        except urllib.error.HTTPError as e:
            state = "allow" if 400 <= e.code < 500 else "deny"
            log(f"   robots.txt at {base}: HTTP {e.code} -> "
                f"{'no rules apply' if state == 'allow' else 'server error, skipping this run'}")
        except Exception as e:
            state = "deny"
            log(f"   robots.txt at {base} unreachable ({e.__class__.__name__}); skipping this run")
        _robots[base] = state
    s = _robots[base]
    if s == "allow":
        return True
    if s == "deny":
        return False
    ok = s.can_fetch(ROBOTS_AGENT, url)
    if not ok:
        log(f"   robots.txt at {base} disallows {u.path}")
    return ok


def unreachable(url):
    """True when this run couldn't reach the site at all (robots.txt lookup failed)."""
    u = urlparse(url)
    return _robots.get(f"{u.scheme}://{u.netloc}") == "deny"


async def pause():
    await asyncio.sleep(DELAY + random.uniform(0, DELAY))


async def goto(page, url):
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=35000)
        await page.wait_for_timeout(1200)
        return True
    except Exception as e:
        log(f"   ! could not load {url}: {e.__class__.__name__}")
        return False


async def load_all(page, rounds=20):
    """Load the whole list: scroll to the bottom, and click "Load more"/"Show more" buttons,
    until the page stops growing."""
    last = 0
    for _ in range(rounds):
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(1500)
        try:
            clicked = await page.evaluate(P.LOAD_MORE_JS)
        except Exception:
            clicked = False  # a click that navigated away; stop here
        if clicked:
            await page.wait_for_timeout(2000)
        height = await page.evaluate("document.body.scrollHeight")
        if height == last and not clicked:
            break
        last = height


async def scrape_listing(page, src, url_key="listing_url", min_items=3):
    url_t = src[url_key]
    pages = range(1, MAX_LISTING_PAGES + 1) if "{page}" in url_t else [1]
    items, seen, key = [], set(), None
    for n in pages:
        url = url_t.format(page=n)
        if not robots_ok(url):
            break
        if not await goto(page, url):
            break
        await load_all(page)
        anchors = await page.evaluate(P.ANCHORS_JS)
        found, key = P.find_items(anchors, url, src.get("item_link_re"), key, min_items)
        found = [i for i in found if i["detail_url"] not in seen]
        if not found:
            break
        for i in found:
            seen.add(i["detail_url"])
        items += found
        await pause()
    return items


async def merged_listing(page, raw_page, src, url_key="listing_url", min_items=3):
    """Read a listing twice: as a browser shows it and as raw HTML with JavaScript off.
    Some sites ship every settlement in the HTML but trim or paginate the list with JavaScript."""
    items = await scrape_listing(page, src, url_key, min_items)
    raw = await scrape_listing(raw_page, src, url_key, min_items)
    if not items and not raw:
        # Helps tell a layout problem from a bot-check page ("Just a moment...", "Access denied").
        try:
            title = (await page.title())[:80]
        except Exception:
            title = "?"
        log(f"   no settlement links on {src[url_key].format(page=1)} (page title: {title!r})")
    seen = {i["detail_url"] for i in items}
    return items + [i for i in raw if i["detail_url"] not in seen]


async def scrape_cards(page, src, stats):
    """Card-mode sites: every settlement's details and claim button sit on the listing itself."""
    url = src["listing_url"].format(page=1)
    if not robots_ok(url) or not await goto(page, url):
        return []
    await load_all(page)
    cards = await page.evaluate(P.CARDS_JS, src["cards"]["link_text"])
    items, seen = [], set()
    for c in cards:
        if c["href"] in seen:
            continue  # featured cards repeat further down the list
        seen.add(c["href"])
        p = P.parse_card(c)
        items.append({"detail_url": c["href"], "title": p["page_title"], "parsed": p})
    stats["listed"] = stats["claim_link"] = len(items)
    stats["no_proof"] = sum(1 for i in items if i["parsed"]["no_proof"] is True)
    if not items:
        log(f"   no cards with '{src['cards']['link_text']}' on {url} (page title: {(await page.title())[:80]!r})")
    return items


async def scrape_source(ctx, src, cache, aggregator_hosts, max_details, raw_ctx):
    name = src["name"]
    page = await ctx.new_page()
    raw_page = await raw_ctx.new_page()
    if src.get("cards"):
        stats = {"listed": 0, "fetched": 0, "claim_link": 0, "no_proof": 0, "skipped_robots": 0}
        try:
            items = await scrape_cards(page, src, stats)
            log(f"[{name}] {len(items)} settlement cards")
        except Exception as e:
            log(f"[{name}] failed: {e!r}")
            items = []
        finally:
            await page.close()
            await raw_page.close()
        return items, stats
    stats = {"listed": 0, "fetched": 0, "claim_link": 0, "no_proof": 0, "skipped_robots": 0}
    try:
        items = await merged_listing(page, raw_page, src)
        stats["listed"] = len(items)
        log(f"[{name}] {len(items)} listings")
        # Optional: a listing page of only no-proof settlements, which is more reliable than page text.
        no_proof_urls = set()
        if src.get("no_proof_url"):
            no_proof_urls = {i["detail_url"] for i in await merged_listing(page, raw_page, src, "no_proof_url", 1)}
            log(f"[{name}] {len(no_proof_urls)} on its no-proof list")
        now = time.time()
        for it in items:
            url = it["detail_url"]
            c = cache.get(url)
            fresh = (c and c.get("v") == PARSE_VERSION
                     and now - c.get("fetched_at", 0) < REFRESH_DAYS * 86400)
            if not fresh and stats["fetched"] < max_details:
                if not robots_ok(url):
                    stats["skipped_robots"] += 1
                    continue
                if await goto(page, url):
                    d = await page.evaluate(P.DETAIL_JS)
                    anchors = await page.evaluate(P.ANCHORS_JS)
                    parsed = P.parse_detail(d, anchors, aggregator_hosts)
                    cache[url] = {**parsed, "listing_title": it["title"], "fetched_at": now, "v": PARSE_VERSION}
                    stats["fetched"] += 1
                await pause()
            if url in cache:
                if url in no_proof_urls:
                    cache[url]["no_proof"] = True
                cache[url]["last_seen"] = now
                cache[url]["source"] = name
                it["parsed"] = cache[url]
                stats["claim_link"] += bool(cache[url].get("claim_url"))
                stats["no_proof"] += cache[url].get("no_proof") is True
    except Exception as e:
        log(f"[{name}] failed: {e!r}")
    finally:
        await page.close()
        await raw_page.close()
    return items, stats


async def read_form(page):
    """The page's form, giving script-built forms a few extra seconds to appear."""
    res = await page.evaluate(P.FORM_JS)
    if sum(1 for i in res["inputs"] if i.get("visible")) < 3 and not P.BLOCKED_TEXT_RE.search(res["text"][:3000]):
        await page.wait_for_timeout(3500)
        res = await page.evaluate(P.FORM_JS)
    return res


async def check_live(page, url):
    """The claim form on the live site. If the page is a settlement homepage, follow its 'File a
    claim' link once; if the claim URL is an inner page (FAQ, documents) with no such link, look
    for one on the homepage. Returns (result, blocked, admin, notice_docs)."""
    if not await goto(page, url):
        return None, False, None, []
    res = await read_form(page)
    if P.BLOCKED_TEXT_RE.search(res["text"][:3000]):
        return None, True, None, []  # a bot check: the rest of this site will be the same
    html = [await page.content()]
    first = P.classify_form(res)
    anchors = await page.evaluate(P.ANCHORS_JS)
    docs = P.find_notice_docs(anchors, page.url)
    # A page that already shows an ID/PIN box is the answer; don't wander off it.
    if first == "required" or sum(1 for i in res["inputs"] if i.get("visible")) >= 3:
        return first, False, P.detect_admin(" ".join(html)), docs
    link = P.find_claim_link_on_site(anchors, page.url)
    u = urlparse(page.url)
    if not link and u.path not in ("", "/"):
        home = f"{u.scheme}://{u.netloc}/"
        if robots_ok(home) and await goto(page, home):
            anchors = await page.evaluate(P.ANCHORS_JS)
            html.append(await page.content())
            docs += P.find_notice_docs(anchors, page.url)
            link = P.find_claim_link_on_site(anchors, page.url)
    result = first
    if link and robots_ok(link) and await goto(page, link):
        res = await read_form(page)
        html.append(await page.content())
        docs += P.find_notice_docs(await page.evaluate(P.ANCHORS_JS), page.url)
        result = P.classify_form(res) or first
    return result, False, P.detect_admin(" ".join(html)), list(dict.fromkeys(docs))


def fetch_pdf_text(url, max_bytes=8_000_000, max_pages=20):
    from pypdf import PdfReader  # only needed here
    import io
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read(max_bytes)
    reader = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in reader.pages[:max_pages])


async def check_docs(page, docs):
    """The settlement's FAQ page and long-form notice. Returns (result, admin)."""
    admin = None
    for d in docs[:3]:
        if not robots_ok(d):
            continue
        try:
            if urlparse(d).path.lower().endswith(".pdf") or "long_form_notice" in d.lower():
                text = await asyncio.to_thread(fetch_pdf_text, d)
            elif await goto(page, d):
                text = await page.evaluate("document.body ? document.body.innerText : ''")
            else:
                # Often a PDF behind a link that doesn't end in .pdf (the browser tries to download it).
                text = await asyncio.to_thread(fetch_pdf_text, d)
        except Exception as e:
            log(f"   ! could not read {d}: {e.__class__.__name__}")
            continue
        admin = admin or P.detect_admin(text) or P.detect_admin(d)
        result = P.classify_doc_text(text)
        if result:
            return result, admin
    return None, admin


ARCHIVE_LOCK = asyncio.Lock()  # one Internet Archive request at a time, politely spaced


def wayback_html(url):
    """The newest Internet Archive copy of a page, as the raw HTML it was saved with."""
    api = "https://archive.org/wayback/available?url=" + urllib.parse.quote(url, safe="")
    with urllib.request.urlopen(urllib.request.Request(api, headers={"User-Agent": UA}), timeout=30) as r:
        snap = json.load(r).get("archived_snapshots", {}).get("closest")
    if not snap or str(snap.get("status")) != "200":
        return None
    ts = snap["timestamp"]
    raw = snap["url"].replace(f"/web/{ts}/", f"/web/{ts}id_/", 1)
    with urllib.request.urlopen(urllib.request.Request(raw, headers={"User-Agent": UA}), timeout=45) as r:
        return r.read(4_000_000).decode("utf-8", "replace")


async def check_archive(raw_ctx, url):
    """Read the Internet Archive's saved copy of the claim page (and the site's homepage), for
    sites whose bot checks keep the scraper out. Returns (result, admin)."""
    u = urlparse(url)
    admin = None
    for target in dict.fromkeys([url, f"{u.scheme}://{u.netloc}/"]):
        async with ARCHIVE_LOCK:
            try:
                html = await asyncio.to_thread(wayback_html, target)
            except urllib.error.HTTPError as e:
                log(f"   ! archive lookup failed for {target}: HTTP {e.code}")
                html = None
            except Exception as e:
                raise RuntimeError(f"Internet Archive unreachable: {e.__class__.__name__}") from e
            await asyncio.sleep(ARCHIVE_DELAY)
        if not html:
            continue
        admin = admin or P.detect_admin(html)
        page = await raw_ctx.new_page()  # scripts off: just the saved HTML
        try:
            await page.set_content(html, wait_until="domcontentloaded", timeout=20000)
            res = await page.evaluate(P.FORM_JS)
        except Exception:
            continue
        finally:
            await page.close()
        result = P.classify_form(res) or P.classify_doc_text(res.get("text"))
        if result:
            return result, admin
    return None, admin


async def check_form(ctx, raw_ctx, url, archive_budget):
    """Whether a settlement's claim needs the notice ID: the live claim form first, then the
    site's FAQ and notice, then the Internet Archive's saved copy. Returns a forms-cache entry."""
    out = {"result": None, "source": None, "admin": None}
    blocked = failed = False
    if not robots_ok(url):
        failed = unreachable(url)
    else:
        page = await ctx.new_page()
        try:
            result, blocked, admin, docs = await check_live(page, url)
            out.update(result=result, source="form" if result else None, admin=admin)
            if not result and docs:
                result, admin = await check_docs(page, docs)
                out.update(result=result, source="faq" if result else None, admin=out["admin"] or admin)
        except Exception as e:
            log(f"   ! form check failed for {url}: {e.__class__.__name__}")
            failed = True
        finally:
            await page.close()
    if not out["result"] and archive_budget[0] > 0:
        archive_budget[0] -= 1
        try:
            result, admin = await check_archive(raw_ctx, url)
            out.update(result=result, source="archive" if result else None, admin=out["admin"] or admin)
        except Exception as e:
            log(f"   ! archive check failed for {url}: {e.__class__.__name__}")
            failed = True
    out["blocked"] = blocked
    out["failed"] = failed and not out["result"]
    return out


async def check_forms(ctx, raw_ctx, settlements, forms):
    """Fill in forms[claim_url] for settlements not checked recently. Returns how many were checked."""
    now = time.time()
    todo = [s["claim_url"] for s in settlements
            if forms.get(s["claim_url"], {}).get("v") != FORM_VERSION
            or now - forms.get(s["claim_url"], {}).get("checked_at", 0) > FORM_RECHECK_DAYS * 86400]
    todo = list(dict.fromkeys(todo))[:MAX_FORM_CHECKS]
    sem = asyncio.Semaphore(FORM_CONCURRENCY)
    archive_budget = [MAX_ARCHIVE_CHECKS]

    async def one(url):
        async with sem:
            entry = await check_form(ctx, raw_ctx, url, archive_budget)
            if entry.pop("failed"):
                # A network problem, not an answer: keep any older result and try again next run.
                log(f"   ! couldn't check {url} this run; will retry")
            else:
                forms[url] = {**entry, "checked_at": time.time(), "v": FORM_VERSION}
            await pause()

    await asyncio.gather(*[one(u) for u in todo])
    return len(todo)


VERIFIED_SOURCES = ("form", "faq", "archive")


def combine_notice(listing_values, form_entry):
    """The claim form (or the site's own FAQ/notice, or a saved copy of the form) is the real
    requirement, so it wins. Otherwise the listing sites: an explicit 'you can file without it'
    beats a generic 'you'll need your ID'."""
    if form_entry.get("result"):
        return form_entry["result"], form_entry.get("source") or "form"
    for v in ("none", "optional", "required"):
        if v in listing_values:
            return v, "listing"
    return None, None


def infer_by_administrator(settlements):
    """Settlements still unknown get 'likely required' when their claims administrator's checked
    settlements nearly all require the ID. Learned from this run's data, not hard-coded.
    Returns {administrator: (required, checked)} for the run summary."""
    tally = {}
    for s in settlements:
        a = s.get("administrator")
        if a and s["notice_source"] in VERIFIED_SOURCES:
            req, n = tally.get(a, (0, 0))
            tally[a] = (req + (s["notice_id"] == "required"), n + 1)
    likely = {a for a, (req, n) in tally.items() if n >= ADMIN_MIN_CHECKED and req / n >= ADMIN_MIN_SHARE}
    for s in settlements:
        if s["notice_id"] is None and s.get("administrator") in likely:
            s["notice_id"], s["notice_source"] = "required", "admin"
    return tally


def pick_title(p):
    t = (p.get("page_title") or "").strip()
    if 10 <= len(t) <= 160:
        return t
    return (p.get("listing_title") or t)[:160]


def merge(results, previous):
    """Combine every source's settlements into one list, deduplicated by official claim site."""
    today = date.today().isoformat()
    first_seen = {s["id"]: s.get("first_seen", today) for s in previous.get("settlements", [])}
    merged = {}
    for name, items in results:
        for it in items:
            p = it.get("parsed")
            if not p or not p.get("claim_url") or p.get("not_settlement"):
                continue
            if p.get("deadline") and p["deadline"] < today:
                continue
            sid = P.settlement_id(p["claim_url"])
            r = merged.get(sid)
            if r is None:
                r = merged[sid] = {
                    "id": sid, "title": pick_title(p), "category": p["category"],
                    "payout": p.get("payout"), "payout_max": p.get("payout_max"),
                    "no_proof_payout": p.get("no_proof_payout"), "no_proof_payout_max": p.get("no_proof_payout_max"),
                    "deadline": p.get("deadline"), "no_proof": p.get("no_proof"),
                    "summary": p.get("summary") or "", "claim_url": p["claim_url"],
                    "sources": [], "first_seen": first_seen.get(sid, today),
                    "_notice": [], "_admin": [],
                }
            else:
                r["payout"] = r["payout"] or p.get("payout")
                if p.get("payout_max") and (r["payout_max"] or 0) < p["payout_max"]:
                    r["payout_max"], r["payout"] = p["payout_max"], p.get("payout")
                if p.get("no_proof_payout") and not r["no_proof_payout"]:
                    r["no_proof_payout"], r["no_proof_payout_max"] = p["no_proof_payout"], p.get("no_proof_payout_max")
                if p.get("deadline") and (not r["deadline"] or p["deadline"] < r["deadline"]):
                    r["deadline"] = p["deadline"]  # earliest reported deadline, to be safe
                if p.get("no_proof") is True:
                    r["no_proof"] = True          # any site reporting a no-proof option wins
                elif r["no_proof"] is None:
                    r["no_proof"] = p.get("no_proof")
                if len(p.get("summary") or "") > len(r["summary"]):
                    r["summary"] = p["summary"]
            r["sources"].append({"name": name, "url": it["detail_url"], "no_proof": p.get("no_proof")})
            if p.get("notice_id"):
                r["_notice"].append(p["notice_id"])
            if p.get("administrator"):
                r["_admin"].append(p["administrator"])
    out = list(merged.values())
    for r in out:
        r["new"] = r["first_seen"] == today
        # Recomputed from the text so payout rules apply without re-reading every page.
        r["payout_max"] = P.payout_max(r["payout"])
        r["no_proof_payout_max"] = P.payout_max(r["no_proof_payout"])
    out.sort(key=lambda r: (-(r["payout_max"] or -1), r["deadline"] or "9999"))
    return out


def load_json(path, default):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


async def daily():
    sources = [s for s in load_json(SOURCES, {})["sources"] if s.get("enabled", True)]
    cache = load_json(CACHE, {})
    previous = load_json(OUT, {})
    agg = KNOWN_AGGREGATORS | {P.host_of(s["listing_url"]) for s in sources}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(user_agent=UA)
        raw_ctx = await browser.new_context(user_agent=UA, java_script_enabled=False)
        runs = await asyncio.gather(*[scrape_source(ctx, s, cache, agg, MAX_DETAILS_PER_RUN, raw_ctx) for s in sources])
        await browser.close()

    results = [(s["name"], items) for s, (items, _) in zip(sources, runs)]
    settlements = merge(results, previous)

    # Check each settlement's claim form for a notice-ID field (cached, a few at a time).
    forms = cache.get(FORMS_KEY, {})
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        fctx = await browser.new_context(user_agent=UA)
        frawctx = await browser.new_context(user_agent=UA, java_script_enabled=False)
        checked = await check_forms(fctx, frawctx, settlements, forms)
        await browser.close()
    live = {s["claim_url"] for s in settlements}
    cache[FORMS_KEY] = {u: v for u, v in forms.items() if u in live}  # forget closed settlements
    forms = cache[FORMS_KEY]
    for s in settlements:
        entry = forms.get(s["claim_url"], {})
        s["notice_id"], s["notice_source"] = combine_notice(s.pop("_notice"), entry)
        listed_admins = s.pop("_admin")
        s["administrator"] = entry.get("admin") or (max(set(listed_admins), key=listed_admins.count) if listed_admins else None)
    admin_tally = infer_by_administrator(settlements)

    now = time.time()
    cache = {k: v for k, v in cache.items() if now - v.get("last_seen", now) < PRUNE_DAYS * 86400}
    CACHE.parent.mkdir(exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=0, sort_keys=True))

    doc = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(settlements),
        "no_proof_count": sum(1 for s in settlements if s["no_proof"] is True),
        "settlements": settlements,
    }
    # Guard: if every site failed, keep yesterday's list instead of publishing an empty one.
    if not settlements and previous.get("settlements"):
        log("No settlements found this run; keeping the previous settlements.json")
    else:
        OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False))

    md = ["## Daily scrape", "", f"**{doc['count']}** open settlements "
          f"(**{doc['no_proof_count']}** no proof), "
          f"**{sum(s['new'] for s in settlements)}** new today.", "",
          f"Notice ID: **{sum(s['notice_id'] == 'required' for s in settlements)}** required, "
          f"**{sum(s['notice_id'] == 'optional' for s in settlements)}** optional, "
          f"**{sum(s['notice_id'] == 'none' for s in settlements)}** not asked, "
          f"**{sum(s['notice_id'] is None for s in settlements)}** unclear. "
          f"Claim forms checked this run: **{checked}**. "
          f"Answers from: " + ", ".join(f"{k} **{sum(s['notice_source'] == k for s in settlements)}**"
                                         for k in ("form", "faq", "archive", "listing", "admin")) + ".", "",
          "Administrators (checked settlements needing the ID): " + (", ".join(
              f"{a} {req}/{n}" for a, (req, n) in sorted(admin_tally.items(), key=lambda x: -x[1][1])) or "none yet") + ".", "",
          "| Site | Listed | Pages read | With claim link | No proof |", "|---|---|---|---|---|"]
    for s, (_, st) in zip(sources, runs):
        md.append(f"| {s['name']} | {st['listed']} | {st['fetched']} | {st['claim_link']} | {st['no_proof']} |")
        if st["listed"] == 0:
            md.append(f"| ⚠️ {s['name']} found nothing: run **Test a site** on it | | | | |")
    summary("\n".join(md))
    log("\n".join(md))


async def test(target, limit):
    sources = load_json(SOURCES, {})["sources"]
    src = next((s for s in sources if s["name"] == target), None)
    if src is None:
        if not target.startswith("http"):
            sys.exit(f"'{target}' is not a name in sources.json or a URL")
        src = {"name": "test", "listing_url": target}
    existing = {s["id"] for s in load_json(OUT, {}).get("settlements", [])}
    agg = KNOWN_AGGREGATORS | {P.host_of(s["listing_url"]) for s in sources} | {P.host_of(src["listing_url"])}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        ctx = await browser.new_context(user_agent=UA)
        raw_ctx = await browser.new_context(user_agent=UA, java_script_enabled=False)
        items, st = await scrape_source(ctx, src, {}, agg, limit, raw_ctx)
        await browser.close()

    rows = []
    new = dup = 0
    for it in items:
        p = it.get("parsed")
        if not p:
            continue
        sid = P.settlement_id(p["claim_url"]) if p.get("claim_url") else None
        is_new = sid is not None and sid not in existing
        new += is_new
        dup += sid is not None and not is_new
        rows.append(f"| {pick_title(p)[:60]} | {p.get('payout') or '?'} | {p.get('deadline') or '?'} | "
                    f"{ {True: 'no', False: 'yes', None: '?'}[p.get('no_proof')] } | "
                    f"{P.host_of(p['claim_url']) if p.get('claim_url') else '—'} | {'new' if is_new else ''} |")
    md = [f"## Test: {src['name']}", f"`{src['listing_url']}`", "",
          f"- Settlement links found on the listing: **{st['listed']}**",
          f"- Pages read (test limit {limit}): **{st['fetched']}**",
          f"- With an official claim link: **{st['claim_link']}**",
          f"- No proof required: **{st['no_proof']}**",
          f"- Not already in settlements.json: **{new}** (already covered: {dup})", ""]
    if st["listed"] == 0:
        md.append("⚠️ No settlement links detected. Add an `item_link_re` for this site, or check the listing_url.")
    elif st["claim_link"] == 0:
        md.append("⚠️ Pages were read but no official claim links were found on them.")
    if rows:
        md += ["| Settlement | Payout | Deadline | Proof? | Claim site | |", "|---|---|---|---|---|---|"] + rows
    summary("\n".join(md))
    log("\n".join(md))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", metavar="NAME_OR_URL")
    ap.add_argument("--limit", type=int, default=15)
    a = ap.parse_args()
    asyncio.run(test(a.test, a.limit) if a.test else daily())


if __name__ == "__main__":
    main()
