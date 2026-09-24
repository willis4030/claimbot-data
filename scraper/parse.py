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
    re.compile(r"nothing to document|on your word alone|self[- ]attest", re.I),
    re.compile(r"proof\s+required\??\s*\n+\s*(no\b|none|not required)", re.I),
    re.compile(r"(do not|don't|does not|doesn't|will not|won't)\s+(need|have)\s+to\s+(provide|submit|include|upload)\s+"
               r"(any\s+)?(proof|documentation|documents|receipts?)", re.I),
]
PROOF_REQ_RES = [
    re.compile(r"proof(\s+of\s+purchase)?(\s+required)?\s*[:\-]\s*yes\b", re.I),
    re.compile(r"proof(\s+of\s+purchase)?\s+(is\s+)?required", re.I),
]
DEADLINE_RE = re.compile(
    r"(?:claim|filing|submission)?\s*deadline\s*[:\-\u2013]?\s*(?:is\s+)?"
    r"([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}|\d{1,2}/\d{1,2}/\d{2,4})", re.I)
PAYOUT_RE = re.compile(
    r"(?:award|payout|payment|estimated|up to|receive|benefit|flat|cash|claim|get)[^$\n]{0,40}"
    r"(\$[\d,]+(?:\.\d\d)?(?:\s*(?:-|to|\u2013)\s*\$[\d,]+(?:\.\d\d)?)?)", re.I)
MONEY_RE = re.compile(r"\$([\d,]+(?:\.\d\d)?)")
ELIG_RE = re.compile(
    r"eligib|class members?|who (is|are) (included|eligible)|you (may|could) qualify|"
    r"purchased|bought|received (a )?notice|residents?|between .{3,30}\d{4}", re.I)
CLAIM_LINK_TEXT = re.compile(
    r"\b(file|submit|start|make|begin)\s+(a\s+|your\s+)?claim|claim\s+form|"
    r"settlement\s+(web)?site|official\s+(settlement\s+)?(web)?site", re.I)
# Never a claim site. Matched against the whole domain, so "t.co" can't match "settlement.com".
SKIP_DOMAINS = ("facebook.com", "twitter.com", "x.com", "linkedin.com", "pinterest.com", "reddit.com",
                "youtube.com", "youtu.be", "instagram.com", "tiktok.com", "google.com", "amazon.com",
                "apple.com", "bit.ly", "t.co", "courtlistener.com", "uscourts.gov", "justia.com",
                "law360.com", "threads.net")


def on_domain(host, domains):
    """True if host is one of the domains or a subdomain of one (files.example.com -> example.com)."""
    return any(host == d or host.endswith("." + d) for d in domains)
# Site pages that are never settlements: top-level ones (/privacy, /about...) and WordPress-style
# archive folders anywhere in the path (/category/..., /page/2). A /settlements/privacy/x link is kept.
NAV_RE = re.compile(r"^/(about|contact|privacy|privacy-policy|terms|login|signin|sign-up|signup|account|faq|"
                    r"search|newsletter|cart|blog)(/|$|\.)|/(category|categories|tag|tags|author|page|feed)(/|$)", re.I)
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


LABELED_PROOF_RE = re.compile(
    r"(?:is\s+)?proof(?:\s+of\s+purchase)?\s+required\??\s*:?\s*\n+\s*([^\n]{1,40})", re.I)


def detect_no_proof(text):
    """1 = no proof needed, 0 = proof required, None = unknown.

    A labeled field ("Is Proof Required?" followed by its answer) wins over anything else on
    the page, so tags on related-settlement cards elsewhere on the page can't mislead it.
    """
    m = LABELED_PROOF_RE.search(text)
    if m:
        v = m.group(1).strip().lower()
        if re.match(r"(no\b|none|not required|n/a\b)", v) and not v.startswith("n/a"):
            return True
        if re.match(r"(yes\b|proof required|required)", v):
            return False
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


LABELED_PAYOUT_RE = re.compile(
    r"^\s*(?:estimated\s+payout(?:\s+per\s+(?:person|claimant|class member))?|estimated\s+award|award|payout|"
    r"potential\s+award|cash\s+payment|benefit)\s*:?\s*\n+\s*([^\n]{2,90})$", re.I | re.M)
FUND_LINE_RE = re.compile(r"attorney|fees|costs|service award|administration|settlement fund|agreed to pay|"
                          r"million|billion|\$[\d,]{9,}", re.I)


def parse_payout(text):
    """What a claimant can expect: the no-proof amount if stated, else a labeled payout field,
    else a general payout sentence (skipping lines about the fund, fees and costs)."""
    def clean(v):
        v = v.strip()
        # Drop a category tag glued onto the end ("$7 per purchaseShampoo", "$2,000Real Estate").
        v = re.sub(r"(?<=[a-z\d])[A-Z][a-z]+(?:\s(?:&\s)?[A-Z][a-z]+)*$", "", v)
        return v.rstrip(",.;:· ").strip()[:90]
    # The page's own labeled field is the most reliable, and it comes before any
    # related-settlement lists further down the page.
    m = LABELED_PAYOUT_RE.search(text)
    if m:
        return clean(m.group(1))
    m = NO_PROOF_AMOUNT_RES[0].search(text)
    if m:
        return clean(m.group(1))
    m = NO_PROOF_AMOUNT_RES[1].search(text)
    if m:
        return clean(m.group(3))
    for line in text.splitlines():
        if FUND_LINE_RE.search(line):
            continue
        m = PAYOUT_RE.search(line)
        if m:
            return clean(m.group(1))
    return None


# "Pro rata share of $10,000,000", "Equal share of a $6.3M fund": the figure is the whole
# fund, not what one person gets, so it can't be used as a per-person amount.
FUND_SHARE_RE = re.compile(
    r"\b(share|portion|split)\s+of\s+(an?\s+|the\s+)?\$|\bfund\b|\$[\d.,]+\s*(m|mm|million|b|billion)\b", re.I)


def payout_max(payout):
    """Largest per-person dollar figure in a payout string, for sorting and the minimum-payout
    filter. None when the only figure is a fund total."""
    if not payout:
        return None
    if FUND_SHARE_RE.search(payout):
        return None
    vals = [float(v.replace(",", "")) for v in MONEY_RE.findall(payout)]
    if not vals:
        return None
    # A "pro rata" figure in the hundreds of thousands is a fund total, not a per-person cap.
    if re.search(r"pro\s*rata|equal\s+share", payout, re.I) and max(vals) >= 100_000:
        return None
    return max(vals)


def categorize(text):
    for name, pat in CATEGORIES:
        if re.search(pat, text, re.I):
            return name
    return "Product"


# Headings where a listing site's own page ends and its lists of other settlements begin.
BOUNDARY_RE = re.compile(
    r"^\s*(?:(?:more|related|trending|recent|latest|other|similar|popular|top|you may also like)\b[^\n]{0,50}?"
    r"\b(?:settlements?|class actions?|lawsuits?|claims|articles|posts|news)|trending|related)\s*:?\s*$",
    re.I | re.M)


def main_text(text):
    """The page up to where lists of other settlements begin."""
    m = BOUNDARY_RE.search(text)
    return text[:m.start()] if m else text


# Newsletter, privacy and navigation text that ends up inside page content.
BOILERPLATE_RE = re.compile(
    r"newsletter|subscribe|unsubscribe|inbox|sign up|join (thousands|our)|free settlement alerts|"
    r"notice at collection|privacy (choices|policy|notice)|cookies?\b|advertis|"
    r"see who qualifies|read more|learn more|filing is free|takes a few minutes|\u2192|->", re.I)

# A line that says some payment needs no documentation.
NO_DOC_LINE_RE = re.compile(
    r"no\s+(documentation|documents|proof|receipts?)(\s+of\s+purchase)?(\s+(is|are))?\s+(required|needed|necessary)|"
    r"without\s+(any\s+)?(proof|documentation|documents|receipts?)|nothing to document|no[\s-]proof|"
    r"(do not|don't|does not|doesn't)\s+(need|have)\s+to\s+(provide|submit|include|upload)", re.I)
NP_MONEY_RE = re.compile(
    r"\$[\d,]+(?:\.\d\d)?(?:\s*(?:,\s*)?(?:or|to|-|\u2013)\s*\$[\d,]+(?:\.\d\d)?)?")


def parse_no_proof_payout(main):
    """The amount a claimant gets without documents, when the page states one.

    Works sentence by sentence: the amount in the same sentence as the no-proof wording
    wins; if that sentence names no amount ("No documentation is required for this
    payment."), the sentence just before it in the same paragraph is used.
    """
    for line in re.split(r"\n+", main):
        if not NO_DOC_LINE_RE.search(line) or FUND_LINE_RE.search(line):
            continue
        sentences = re.split(r"(?<=[.;!?])\s+(?=[A-Z(])", line)
        for i, sent in enumerate(sentences):
            if not NO_DOC_LINE_RE.search(sent):
                continue
            # Amount right before the phrase ("$100 with no documentation"), then right
            # after it ("without documentation can claim $50"), then any amount in the sentence.
            phrase = NO_DOC_LINE_RE.search(sent)
            before = list(NP_MONEY_RE.finditer(sent[max(0, phrase.start() - 45):phrase.start()]))
            if before:
                amt = before[-1].group(0)
            else:
                m2 = NP_MONEY_RE.search(sent, phrase.end()) or NP_MONEY_RE.search(sent)
                if not m2 and i > 0:
                    m2 = NP_MONEY_RE.search(sentences[i - 1])
                if not m2:
                    continue
                amt = m2.group(0)
            return re.sub(r"\s*,\s*(or|to)\s*", r" \1 ", amt).rstrip(",. ")
    return None


def eligibility_summary(paragraphs, limit=1400):
    paragraphs = [p for p in paragraphs if not BOILERPLATE_RE.search(p)]
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


def find_items(anchors, listing_url, item_link_re=None, lock_key=None, min_items=3):
    """Pick the links on a listing page that point to individual settlement pages.

    With item_link_re, use it. Otherwise auto-detect: settlement pages on a site
    share a URL shape (same folder with a long-slug, or the same script with an id),
    and they are the most numerous such group on the listing page.
    """
    host = host_of(listing_url)
    groups = {}
    pat = re.compile(item_link_re, re.I) if item_link_re else None
    for a in anchors:
        if a.get("menu"):
            continue  # site menus, headers and footers are never the settlement list
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
            if u.query and "-" not in segs[-1]:
                key = "q:" + path          # e.g. /settlement.php?id=123
            elif "-" in segs[-1]:
                # Group by top folder, so /settlements/x and /settlements/data-breaches/y count together.
                key = "p:" + (segs[0] if len(segs) > 1 else "")
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
        # Most links wins; links inside a folder (/settlements/x) beat top-level pages (/x) on ties or near-ties.
        key = max(groups, key=lambda k: len(groups[k]) * (1.5 if k.startswith("p:") and k != "p:" else 1))
        if not pat and len(groups[key]) < min_items:
            return [], None
    return [{"detail_url": h, "title": t} for h, t in groups[key].items() if len(t) >= 12], key


def pick_claim_link(anchors, aggregator_hosts):
    def ok(href):
        h = host_of(href)
        return (href.startswith("http") and bool(h) and not on_domain(h, aggregator_hosts)
                and not on_domain(h, SKIP_DOMAINS))
    def pdf(href):
        return urlparse(href).path.lower().endswith(".pdf")
    for want_pdf in (False, True):
        for a in anchors:
            if ok(a["href"]) and pdf(a["href"]) == want_pdf and CLAIM_LINK_TEXT.search(a["text"]):
                return a["href"]
    for a in anchors:
        if ok(a["href"]) and not pdf(a["href"]) and "settlement" in host_of(a["href"]):
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
  href: a.href,
  text: (a.innerText || a.getAttribute('aria-label') || a.title || '').trim().replace(/\\s+/g, ' '),
  menu: !!a.closest('nav, header, footer, [role=navigation], .navbar, .nav-menu, .w-nav')
}))"""

DETAIL_JS = """() => {
  // Hide site-wide menus so their links (e.g. a "No Proof" menu item) don't count as page content.
  document.querySelectorAll('nav, header, footer, [role=navigation], .navbar, .nav-menu, .w-nav')
    .forEach(e => { e.style.display = 'none'; });
  const root = document.body;
  // Where the page's lists of other settlements begin ("More Class Actions", "Trending"...).
  const boundaryRe = /^((more|related|trending|recent|latest|other|similar|popular|top|you may also like)\\b.{0,50}?\\b(settlements?|class actions?|lawsuits?|claims|articles|posts|news)|trending|related)\\s*:?$/i;
  const boundary = Array.from(root.querySelectorAll('h2, h3, h4, h5'))
      .find(h => h.offsetParent !== null && boundaryRe.test(h.innerText.trim()));
  const before = (e) => !boundary || (boundary.compareDocumentPosition(e) & Node.DOCUMENT_POSITION_PRECEDING);
  const paras = Array.from(root.querySelectorAll('p, li, td, dd'))
      .filter(e => e.offsetParent !== null && before(e))
      .map(e => e.innerText.trim().replace(/\\s+/g, ' ')).filter(Boolean);
  const h1 = document.querySelector('h1');
  return { title: h1 ? h1.innerText.trim() : document.title, text: root.innerText, paras };
}"""


def parse_detail(d, anchors, aggregator_hosts):
    text = d["text"]
    main = main_text(text)
    payout = parse_payout(main)
    no_proof_payout = parse_no_proof_payout(main)
    return {
        "page_title": d["title"],
        "claim_url": pick_claim_link(anchors, aggregator_hosts),
        "deadline": parse_deadline(text),
        "payout": payout,
        "payout_max": payout_max(payout),
        "no_proof_payout": no_proof_payout,
        "no_proof_payout_max": payout_max(no_proof_payout),
        "no_proof": detect_no_proof(text),
        "category": categorize(d["title"] + "\n" + text[:3000]),
        "summary": eligibility_summary(d["paras"]),
        "not_settlement": bool(NOT_SETTLEMENT_RE.search(d["title"])),
        "notice_id": classify_notice(main),
    }


# ---------------------------------------------------------------- card mode
# Some sites list every settlement as a card with its details and a direct button to the
# official claim site, with no page of its own. CARDS_JS finds those buttons and returns the
# text of the card around each one.
CARDS_JS = """(linkText) => {
  const re = new RegExp(linkText, 'i');
  const links = Array.from(document.querySelectorAll('a[href]')).filter(a => re.test(a.innerText || ''));
  const out = [];
  for (const a of links) {
    // Climb to the largest ancestor that still contains only this one button: that's the card.
    let card = a;
    while (card.parentElement && card.parentElement !== document.body) {
      const n = Array.from(card.parentElement.querySelectorAll('a[href]')).filter(x => re.test(x.innerText || '')).length;
      if (n > 1) break;
      card = card.parentElement;
    }
    const h = card.querySelector('h1, h2, h3, h4, h5, [class*=title]');
    out.push({ href: a.href, title: (h ? h.innerText : '').trim(), text: card.innerText });
  }
  return out;
}"""

CARD_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b")
CARD_MONEY_RE = re.compile(r"(\$[\d,.]+\+?(?:\s*[-\u2013]\s*\$[\d,.]+\+?)?|\bvaries\b)", re.I)
CARD_PROOF_RE = re.compile(r"required\?[\s\S]{0,160}?\b(yes|no|n/a)\b", re.I)
CARD_NOISE_RE = re.compile(r"visit official settlement website|\bshare\b|\d+ days left|featured|"
                           r"settlement\s+payout|deadline|proof\s+required\?|\bvaries\b|\bn/a\b", re.I)


def parse_card(card):
    """Turn one listing card's text into the same fields a settlement page gives."""
    text = card["text"]
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    title = card.get("title") or (lines[0] if lines else "")
    after = re.split(r"required\?", text, maxsplit=1, flags=re.I)[-1]
    m = CARD_MONEY_RE.search(after)
    payout = m.group(1).rstrip(",.;:") if m else None
    if payout and payout.lower() == "varies":
        payout = "Varies"
    deadline = None
    d = CARD_DATE_RE.search(after)
    if d:
        try:
            deadline = dateparser.parse(d.group(1)).date().isoformat()
        except (ValueError, OverflowError):
            pass
    pr = CARD_PROOF_RE.search(text)
    no_proof = {"no": True, "yes": False}.get(pr.group(1).lower()) if pr else detect_no_proof(text)
    summary_lines = [l for l in lines if len(l) > 25 and l != title and "\t" not in l
                     and not re.search(r"proof\s+required\?", l, re.I)
                     and not CARD_NOISE_RE.fullmatch(l) and not CARD_MONEY_RE.fullmatch(l)
                     and not CARD_DATE_RE.fullmatch(l)]
    summary = " ".join(summary_lines)
    return {
        "page_title": title,
        "listing_title": title,
        "claim_url": card["href"],
        "deadline": deadline,
        "payout": payout,
        "payout_max": payout_max(payout),
        "no_proof": no_proof,
        "category": categorize(title + "\n" + summary),
        "summary": summary[:1400],
        "not_settlement": bool(NOT_SETTLEMENT_RE.search(title)),
        "notice_id": classify_notice(text),
    }


# ---------------------------------------------------------------- "Load more" buttons
LOAD_MORE_JS = """() => {
  const re = /^\\s*(load|show|view|see)\\s+more\\b/i;
  const els = Array.from(document.querySelectorAll('button, [role=button], a[href="#"], a:not([href])'));
  const el = els.find(e => re.test(e.innerText || '') && e.offsetParent !== null && !e.disabled);
  if (!el) return false;
  el.scrollIntoView(); el.click(); return true;
}"""


# ---------------------------------------------------------------- notice ID / PIN
# Many settlements mail a notice with a Class Member ID or PIN. These decide whether you
# need it to file: "required", "optional", "none" (the form doesn't ask), or None (unclear).

_ID = r"(class\s*member|claimant|notice|unique|claim|settlement|member)\s*(id|identification|number|code|#)|\bpin\b"
NOTICE_OPTIONAL_RE = re.compile(
    r"(did\s*n[o']t|do\s*n[o']t|did not|do not|never)\s+(receive|get|got)\s+(a|the|an)?\s*(notice|email|postcard|letter)[^.]{0,80}"
    r"\b(can|may|still|also)\b|"
    r"without\s+(a|the|an)\s+(notice|" + _ID + r")|"
    r"(" + _ID + r")\s*(is|are)?\s*(optional|not required)|"
    r"(even\s+if|whether\s+or\s+not)\s+you\s+(did\s*n[o']t|did not)?\s*(receive|get)\s+(a|the)?\s*notice", re.I)
NOTICE_NONE_RE = re.compile(
    r"(you\s+)?(do\s*n[o']t|do not|don't)\s+need\s+(a|the|your|an)\s+(" + _ID + r")", re.I)
NOTICE_REQUIRED_RE = re.compile(
    r"only\s+(class\s+members|people|persons|individuals|those)\s+who\s+(received|were\s+sent|got)\s+(a|the|direct)?\s*notice|"
    r"(need|must\s+(enter|provide|include|have|use)|required\s+to\s+(enter|provide))\s+(your|the|a)\s+(" + _ID + r")|"
    r"(" + _ID + r")\s*(is|are)\s+required|"
    r"requires?\s+(your|the|a)\s+(" + _ID + r")", re.I)


def classify_notice(text):
    """From a listing page's own text. Permissive statements win over generic 'you'll need' ones."""
    if not text:
        return None
    if NOTICE_NONE_RE.search(text):
        return "none"
    if NOTICE_OPTIONAL_RE.search(text):
        return "optional"
    if NOTICE_REQUIRED_RE.search(text):
        return "required"
    return None


# Reads a claim form: every field's label, whether it's marked required, and the page text.
FORM_JS = """() => {
  const visible = (e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const skip = ['hidden', 'submit', 'button', 'image', 'reset', 'search'];
  const els = Array.from(document.querySelectorAll('input, select, textarea'))
    .filter(e => !skip.includes((e.getAttribute('type') || '').toLowerCase()));
  const inputs = els.map(e => {
    const parts = [];
    if (e.labels) for (const l of e.labels) parts.push(l.innerText);
    parts.push(e.getAttribute('aria-label'), e.placeholder, e.name, e.id, e.title);
    const lb = e.getAttribute('aria-labelledby');
    if (lb) lb.split(/\\s+/).forEach(id => { const n = document.getElementById(id); if (n) parts.push(n.innerText); });
    const labelText = parts.filter(Boolean).join(' ');
    const desc = labelText.replace(/([a-z])([A-Z])/g, '$1 $2').replace(/[_\\-\\[\\].]+/g, ' ');
    const required = e.required || e.getAttribute('aria-required') === 'true' || /\\*/.test(labelText);
    return { desc, required, visible: visible(e) };
  });
  return { inputs, text: (document.body.innerText || '').slice(0, 20000) };
}"""

FORM_ID_FIELD_RE = re.compile(
    r"(class\s*member|claimant|notice|unique|claim|settlement|member|participant|confirmation)\s*"
    r"(id|identification|number|no\b|code|#)|\bpin\b|control\s*(number|no)|access\s*code|\buid\b", re.I)
FORM_NOT_ID_RE = re.compile(r"phone|zip|postal|card|routing|account|social|ssn|tax|date|email|birth", re.I)
FORM_OPTIONAL_TEXT_RE = re.compile(
    r"(did\s*n[o']t|did not|do\s*n[o']t|do not)\s+(receive|get|have)\s+(a|an|the|my)?\s*(notice|" + _ID + r")|"
    r"(if|whether)\s+you\s+(received|have)\s+(a|an|the)\s+(notice|" + _ID + r")", re.I)


def classify_form(res):
    """From a claim form. None if no real form was found on the page."""
    inputs = res.get("inputs") or []
    id_fields = [i for i in inputs if FORM_ID_FIELD_RE.search(i["desc"]) and not FORM_NOT_ID_RE.search(i["desc"])]
    visible_fields = [i for i in inputs if i.get("visible")]
    has_optional_path = bool(FORM_OPTIONAL_TEXT_RE.search(res.get("text") or ""))
    if id_fields:
        if has_optional_path:
            return "optional"
        return "required" if any(i["required"] for i in id_fields) else "optional"
    if len(visible_fields) >= 3:
        return "none"  # a real form that doesn't ask for an ID
    return None


def find_claim_link_on_site(anchors, current_url):
    """On an official settlement homepage, find its own 'File a Claim' link (same site)."""
    here = urlparse(current_url).netloc
    for a in anchors:
        if CLAIM_LINK_TEXT.search(a.get("text") or "") and urlparse(a["href"]).netloc == here:
            return a["href"]
    return None
