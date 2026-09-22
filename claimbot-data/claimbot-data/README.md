# claimbot-data

Collects open class action settlements from several listing sites once a day and publishes them as one file, `settlements.json`, for the Claimbot app.

The app reads:

```
https://raw.githubusercontent.com/willis4030/claimbot-data/main/settlements.json
```

The file only contains public settlement listings, never anyone's personal information.

## What runs

**Daily scrape** runs every day at 11:00 UTC. For every enabled site in `sources.json` it:

1. Reads the site's listing pages.
2. Detects which links go to individual settlements.
3. Reads each settlement page for its payout, deadline, proof requirement, eligibility, and official claim link.
4. Merges everything into `settlements.json`.

A few rules shape the output:

* The same settlement listed on several sites appears once. Settlements are matched by their official claim website.
* A settlement is dropped if its deadline has passed, it has no official claim link, or it's an investigation rather than a settlement.
* When sites disagree on a deadline, the earliest one is kept.
* Settlement pages are cached for 7 days, so later runs are fast and light on the sites.
* The scraper follows each site's robots.txt and waits between requests.

Each run's summary page shows how many settlements each site produced. If a site produces 0, it's flagged.

To run it right away: go to **Actions**, then **Daily scrape**, then **Run workflow**.

## Adding a site

1. Open `sources.json` and tap the pencil icon to edit.
2. Add an entry:
   ```json
   { "name": "newsite", "enabled": true, "listing_url": "https://newsite.com/settlements" }
   ```
   If the site has numbered pages, put `{page}` where the number goes, e.g. `https://newsite.com/settlements?page={page}`.
3. Commit the change.
4. Go to **Actions**, then **Test a site**, then **Run workflow**, and enter the site's name. The results page shows:
   * How many settlement links it found
   * How many have an official claim link
   * How many need no proof
   * How many are **new**, meaning not already covered by the other sites
5. Leave the site enabled if the results look good. Otherwise, set `"enabled": false`.

You can also test a site before adding it, by entering its full listing URL instead of a name.

If a test finds 0 settlement links, the site uses an unusual link format. Add an `item_link_re` (a regex that matches its settlement page URLs) to that site's entry. For example: `"item_link_re": "/settlement/[^/]+$"`.

## Files

| File | What it is |
|---|---|
| `sources.json` | The list of listing sites |
| `settlements.json` | The output the app reads (written by the daily run) |
| `data/cache.json` | Parsed settlement pages, so they aren't re-read every day |
| `scraper/run.py` | Daily run and site test |
| `scraper/parse.py` | Link detection and page parsing |
| `.github/workflows/` | The two Actions jobs |

## settlements.json format

```json
{
  "updated": "2026-09-23T11:14:02+00:00",
  "count": 212,
  "no_proof_count": 131,
  "settlements": [
    {
      "id": "3f9a1c0b2d4e",
      "title": "Example Data Breach Settlement",
      "category": "Data breach",
      "payout": "$100",
      "payout_max": 100.0,
      "deadline": "2026-11-09",
      "no_proof": true,
      "summary": "Who qualifies…",
      "claim_url": "https://examplesettlement.com/claim",
      "sources": [{ "name": "claimdepot", "url": "https://…" }],
      "first_seen": "2026-09-23",
      "new": true
    }
  ]
}
```

Notes on the fields:

* `no_proof` is `true`, `false`, or `null` (couldn't tell).
* `payout` and `payout_max` can be `null` when a site lists "varies" or "pro rata."
* `payout` prefers the no-proof amount when a page states one.
* `deadline` can be `null` if no site states one.
