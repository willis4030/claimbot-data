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
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36 ClaimbotData/1.0 (+https://github.com/willis4030/claimbot-data)")
ROBOTS_AGENT = "ClaimbotData"
MAX_LISTING_PAGES = 6
DELAY = 1.5              # seconds between requests to the same site (plus jitter)
REFRESH_DAYS = 7         # re-read a settlement page after this many days
MAX_DETAILS_PER_RUN = 500
PRUNE_DAYS = 30          # forget cached pages not listed anywhere for this long
PARSE_VERSION = 4        # bump when parsing changes, so cached pages are re-read
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
