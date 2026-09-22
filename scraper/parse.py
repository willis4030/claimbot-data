"""Parsing helpers shared by the daily run and the site test."""
import hashlib
import re
from urllib.parse import urlparse

from dateutil import parser as dateparser

# ---------------------------------------------------------------- proof, deadline, payout
NO_PROOF_RES = [
    re.compile(r"no[\s-]+proof(\s+of\s+purchase)?(\s+is)?\s+(required|needed|necessary)", re.I),
    re.compile(r"without\s+(a\s+|any\s+)?(proof|receipts?|documentation)", re.I),
    re.compile(r"proof(\s+of\s+purchase)?\s+(is\s+)?not\s+(required|needed)", re.I),
    re.compile(r"proof(\s+of\s+purchase)?(\s+required)?\s*[:\-]\s*(no\b|none|n/?a\b|not required)", re.I),
    re.compile(r"no\s+(documents?|documentation|receipts?)\s+(needed|required)", re.I),
    re.compile(r"\bno[\s-]proof\b", re.I),
    re.compile(r"nothing to document|on your word alone|self[- ]attest", re.I),
]
PROOF_REQ_RES = [
    re.compile(r"proof(\s+of\s+purchase)?(\s+required)?\s*[:\-]\s*yes\b", re.I),
    re.compile(r"proof(\s+of\s+purchase)?\s+(is\s+)?required", re.I),
]
DEADLINE_RE = re.compile(
    r"(?:claim|filing|submission)?\s*deadline\s*[:\-\u2013]?\s*(?:is\s+)?"
    r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", re.I)
PAYOUT_RE = re.compile(
    r"(?:award|payout|payment|estimated|up to|receive|benefit|flat|cash)[^$\n]{0,40}"
    r"(\$[\d,]+(?:\.\d\d)?(?:\s*(?:-|to|\u2013)\s*\$[\d,]+(?:\.\d\d)?)?)", re.I)
MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d\d)?)")
ELIG_RE = re.compile(
    r"eligib|class members?|who (is|are) (included|eligible)|you (may|could) qualify|"
    r"purchased|bought|received (a )?notice|residents?|between .{3,30}\d{4}", re.I)
CLAIM_LINK_TEXT = re.compile(
    r"\b(file|submit|start|make|begin)\s+(a\s+|your\s+)?claim|claim\s+form|"
    r"settlement\s+(web)?site|official\s+(settlement\s+)?(web)?site", re.I)
SKIP_HOSTS = ("facebook.", "twitter.", "x.com", "linkedin.", "pinterest.", "reddit.", "youtube.",
              "instagram.", "tiktok.", "google.", "amazon.", "apple.com", "bit.ly", "t.co",
              "courtlistener.", "pacer", "justia.", "law360.")
NAV_RE = re.compile(r"/(about|contact|privacy|terms|login|signin|sign-up|signup|account|faq|"
                    r"category|categories|tag|tags|author|page|search|feed|newsletter|cart|blog)(/|$)", re.I)
# Things that are not settlements you can claim.
NOT_SETTLEMENT_RE = re.compile(r"\b(investigation|lawsuit to join|free case review|case review|"
                               r"do you qualify to join)\b", re.I)

CATEGORIES = [
    ("Data breach", r"data (breach|security|incident)|cyber ?attack|ransomware|exposed .{0,30}(information|data)"),
    ("Privacy & tech", r"privacy|biometric|bipa|tracking|pixel|recording|wiretap|cookies|location data|texts?\b|tcpa"),
    ("Subscription & fees", r"subscri|streaming|auto[- ]renew|membership|fees?\b|surcharge|overdraft|late charge"),
    ("Antitrust", r"price[- ]fixing|antitrust|monopol|conspir"),
    ("Auto", r"vehicle|\bcars?\b|truck|automaker|dealership|airbag|engine|transmission"),
    ("Financial", r"\bbank|credit card|loan|mortgage|insurance|brokerage|debt collect"),
    ("Employment", r"employee|wage|overtime|job (posting|applicant)|workers?\b"),
]


def detect_no_proof(text):
    if any(r.search(text) for r in NO_PROOF_RES):
        return True
    if any(r.search(text) for r in PROOF_REQ_RES):
        return False
    return None


def parse_deadline(text):
    m = DEADLINE_RE.search(text)
    if not m:
        return None
    try:
        return dateparser.parse(m.group(1), fuzzy=True).date().isoformat()
    except (ValueError, OverflowError):
        return None


NO_PROOF_AMOUNT_RES = [
    re.compile(r"(\$[\d,]+(?:\.\d\d)?)[^$.\n]{0,30}\b(without|no)\s+(proof|documentation|documents|receipts?)", re.I),
    re.compile(r"\b(without|no)\s+(proof|documentation|documents|receipts?)[^$.\n]{0,40}?(\$[\d,]+(?:\.\d\d)?)", re.I),
]


def parse_payout(text):
    """The payout a no-proof claimant can expect when the page states it, else the general figure."""
    m = NO_PROOF_AMOUNT_RES[0].search(text)
    if m:
        return m.group(1)
    m = NO_PROOF_AMOUNT_RES[1].search(text)
    if m:
        return m.group(3)
    m = PAYOUT_RE.search(text)
    return m.group(1) if m else None


def payout_max(payout):
    """Largest dollar figure in a payout string, for sorting and the minimum-payout filter."""
    if not payout:
        return None
    vals = [float(v.replace(",", "")) for v in MONEY_RE.findall(payout)]
    return max(vals) if vals else None


def categorize(text):
    for name, pat in CATEGORIES:
        if re.search(pat, text, re.I):
            return name
    return "Product"


def eligibility_summary(paragraphs, limit=1400):
    picked = [p for p in paragraphs if ELIG_RE.search(p) and len(p) > 40]
    if not picked:
        picked = [p for p in paragraphs if len(p) > 60][:3]
    out, total = [], 0
    for p in picked:
        if total + len(p) > limit:
            break
        out.append(p)
        total += len(p)
    return "\n\n".join(out)


# ---------------------------------------------------------------- listing pages
def host_of(url):
    return urlparse(url).netloc.lower().removeprefix("www.")


def find_items(anchors, listing_url, item_link_re=None, lock_key=None):
    """Pick the links on a listing page that point to individual settlement pages.

    With item_link_re, use it. Otherwise auto-detect: settlement pages on a site
    share a URL shape (same folder with a long-slug, or the same script with an id),
    and they are the most numerous such group on the listing page.
    """
    host = host_of(listing_url)
    groups = {}
    pat = re.compile(item_link_re, re.I) if item_link_re else None
    for a in anchors:
        href, text = a["href"].split("#")[0], a["text"]
        u = urlparse(href)
        if host_of(href) != host or not u.scheme.startswith("http"):
            continue
        if pat:
            if not pat.search(href):
                continue
            key = "custom"
        else:
            path = u.path.rstrip("/")
            segs = [s for s in path.split("/") if s]
            if not segs or NAV_RE.search(path):
                continue
            if u.query and not segs[-1].count("-") >= 2:
                key = "q:" + path          # e.g. /settlement.php?id=123
            elif segs[-1].count("-") >= 2:
                key = "p:" + "/".join(segs[:-1])   # e.g. /settlements/some-long-slug
            else:
                continue
        g = groups.setdefault(key, {})
        if len(text) > len(g.get(href, "")):
            g[href] = text
    if not groups:
        return [], None
    if lock_key is not None:
        # Later listing pages: keep the link shape learned on page 1, even if only a few remain.
        key = lock_key if lock_key in groups else None
        if key is None:
            return [], lock_key
    else:
        key = max(groups, key=lambda k: len(groups[k]))
        if not pat and len(groups[key]) < 3:
            return [], None
    return [{"detail_url": h, "title": t} for h, t in groups[key].items() if len(t) >= 12], key


def pick_claim_link(anchors, aggregator_hosts):
    def ok(href):
        h = host_of(href)
        return (href.startswith("http") and h and h not in aggregator_hosts
                and not any(s in href for s in SKIP_HOSTS))
    for a in anchors:
        if ok(a["href"]) and CLAIM_LINK_TEXT.search(a["text"]):
            return a["href"]
    for a in anchors:
        if ok(a["href"]) and "settlement" in host_of(a["href"]):
            return a["href"]
    return None


# ---------------------------------------------------------------- identity
SHARED_HOSTS = ("caseinfo", "kccllc", "epiq", "simpluris", "angeion", "jndla", "kroll",
                "gilardi", "rustconsulting", "atticusadmin", "ilym", "cptgroup", "classaction.net")


def settlement_id(claim_url):
    """Same settlement on different listing sites -> same id (by its official site)."""
    u = urlparse(claim_url)
    host = host_of(claim_url)
    key = host
    if any(h in host for h in SHARED_HOSTS):
        seg = u.path.strip("/").split("/")[0].lower()
        key = f"{host}/{seg}"
    return hashlib.sha1(key.encode()).hexdigest()[:12]


ANCHORS_JS = """() => Array.from(document.querySelectorAll('a[href]')).map(a => ({
  href: a.href, text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\\s+/g, ' ')
}))"""

DETAIL_JS = """() => {
  const root = document.querySelector('article, main, .entry-content, #content') || document.body;
  const paras = Array.from(root.querySelectorAll('p, li, td, dd'))
      .map(e => e.innerText.trim().replace(/\\s+/g, ' ')).filter(Boolean);
  const h1 = document.querySelector('h1');
  return { title: h1 ? h1.innerText.trim() : document.title, text: root.innerText, paras };
}"""


def parse_detail(d, anchors, aggregator_hosts):
    text = d["text"]
    payout = parse_payout(text)
    return {
        "page_title": d["title"],
        "claim_url": pick_claim_link(anchors, aggregator_hosts),
        "deadline": parse_deadline(text),
        "payout": payout,
        "payout_max": payout_max(payout),
        "no_proof": detect_no_proof(text),
        "category": categorize(d["title"] + "\n" + text[:3000]),
        "summary": eligibility_summary(d["paras"]),
        "not_settlement": bool(NOT_SETTLEMENT_RE.search(d["title"])),
    }
