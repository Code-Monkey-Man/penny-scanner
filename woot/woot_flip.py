#!/usr/bin/env python3
"""
woot_flip — find Woot deals worth reselling on eBay or locally (FB Marketplace).

Pipeline
  1. Pull live offers from Woot's official developer API (feed -> full offer details).
  2. For each item, search eBay's official Browse API for comparable listings
     in the same condition bucket, filter out junk/accessory matches.
  3. Estimate a realistic resale price, subtract fees, tax, shipping -> net profit + ROI.
  4. Write a ranked HTML report + CSV, with one-tap links to verify on
     eBay SOLD listings, FB Marketplace, Woot, and Amazon.

No third-party packages. Python 3.11+.

Credentials (env vars preferred, or [secrets] in config.toml):
  WOOT_API_KEY        - request one on the Woot forums ("Request Developer API Key" thread)
  EBAY_CLIENT_ID      - developer.ebay.com -> Application Keys (Production App ID)
  EBAY_CLIENT_SECRET  - Production Cert ID

Usage:
  python woot_flip.py                    # live run with config.toml
  python woot_flip.py --demo             # offline run on fake fixture data (tests the pipeline)
  python woot_flip.py --limit 40         # cap number of Woot items evaluated
"""
from __future__ import annotations

import argparse
import base64
import csv
import html
import json
import os
import re
import sqlite3
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# --------------------------------------------------------------------------- config

DEFAULTS = {
    "woot": {
        "feeds": ["All"],
        "skip_categories": ["Shirts", "Gourmet", "Wine"],
        "min_price": 15.0,
        "max_price": 1500.0,
        "shipping_cost": 6.0,          # $0 if you have Amazon Prime linked to Woot
        "sales_tax_rate": 0.0975,      # Clarksville/Montgomery Co. TN combined rate
        "max_pages": 40,
    },
    "ebay": {
        "marketplace": "EBAY_US",
        "results_per_query": 50,
        "max_queries": 300,            # Browse API default quota is 5,000/day
        "min_comps": 5,
    },
    "pricing": {
        "sell_percentile": 0.30,       # price yourself to sell: 30th pct of active listings
        "active_to_sold_haircut": 0.90,  # active asks run above actual sold prices
        "fee_rate": 0.136,             # eBay final value fee, most categories (check yours)
        "fee_fixed": 0.40,             # per-order fee
        "local_price_factor": 0.75,    # FB local sale vs. eBay median item price
        "default_outbound_shipping": 15.0,
        "shipping_by_keyword": {},
    },
    "filters": {
        "min_profit": 25.0,
        "min_roi": 0.25,
    },
    "local": {"fb_city_slug": "nashville"},
    "cache": {"ttl_hours": 12, "path": "cache.sqlite"},
    "secrets": {},
}


def load_config(path: Path | None) -> dict:
    cfg = json.loads(json.dumps(DEFAULTS))
    if path and path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
        for section, vals in user.items():
            if isinstance(vals, dict):
                cfg.setdefault(section, {}).update(vals)
            else:
                cfg[section] = vals
    return cfg


def secret(cfg: dict, name: str) -> str | None:
    return os.environ.get(name) or cfg["secrets"].get(name)


# --------------------------------------------------------------------------- cache / http

class Cache:
    def __init__(self, path: Path, ttl_hours: float):
        self.db = sqlite3.connect(path)
        self.ttl = ttl_hours * 3600
        self.db.execute("CREATE TABLE IF NOT EXISTS http (key TEXT PRIMARY KEY, ts REAL, body TEXT)")
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, first_seen TEXT)")
        self.db.commit()

    def get(self, key: str):
        row = self.db.execute("SELECT ts, body FROM http WHERE key=?", (key,)).fetchone()
        if row and time.time() - row[0] < self.ttl:
            return json.loads(row[1])
        return None

    def put(self, key: str, obj) -> None:
        self.db.execute("INSERT OR REPLACE INTO http VALUES (?,?,?)", (key, time.time(), json.dumps(obj)))
        self.db.commit()

    def first_seen(self, key: str) -> tuple[str, bool]:
        """Returns (first_seen_date, is_new_this_run)."""
        row = self.db.execute("SELECT first_seen FROM seen WHERE key=?", (key,)).fetchone()
        if row:
            return row[0], False
        today = datetime.now().strftime("%Y-%m-%d")
        self.db.execute("INSERT INTO seen VALUES (?,?)", (key, today))
        self.db.commit()
        return today, True


def http_json(method: str, url: str, headers: dict, body: bytes | None = None, retries: int = 4):
    for attempt in range(retries):
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(2 ** attempt * 2)
                continue
            detail = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"{method} {url} -> HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"{method} {url} -> {e.reason}") from None


def notify(title: str, message: str, click: str | None = None, tags: str = "moneybag",
           priority: str = "default") -> bool:
    """Push a phone notification via ntfy.sh. No-op unless NTFY_TOPIC is set."""
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        return False
    headers = {"Title": title.encode("ascii", "ignore").decode(), "Tags": tags, "Priority": priority}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        urllib.request.urlopen(req, timeout=15).read()
        return True
    except Exception as e:  # never fail a run over a notification
        print(f"  ! ntfy failed: {e}")
        return False


# --------------------------------------------------------------------------- woot

class Woot:
    BASE = "https://developer.woot.com"

    def __init__(self, api_key: str, cache: Cache):
        self.key, self.cache, self._last = api_key, cache, 0.0
        self.calls = 0

    def _throttle(self):
        # Woot: 1 req/sec averaged, burst 10, 1,000/day
        wait = 1.05 - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        self.calls += 1

    def _headers(self, json_body=False):
        h = {"Accept": "application/json", "x-api-key": self.key}
        if json_body:
            h["Content-Type"] = "application/json"
        return h

    def feed(self, name: str, max_pages: int) -> list[dict]:
        out, page = [], 1
        while page <= max_pages:
            ck = f"woot:feed:{name}:{page}"
            data = self.cache.get(ck)
            if data is None:
                self._throttle()
                data = http_json("GET", f"{self.BASE}/feed/{name}?page={page}", self._headers())
                self.cache.put(ck, data)
            items = data.get("Items", []) if isinstance(data, dict) else data
            total = data.get("TotalPages", 1) if isinstance(data, dict) else 1
            out.extend(items)
            if page >= total or not items:
                break
            page += 1
        return out

    def offers(self, ids: list[str]) -> dict[str, dict]:
        result, todo = {}, []
        for oid in ids:
            hit = self.cache.get(f"woot:offer:{oid}")
            if hit is not None:
                result[oid] = hit
            else:
                todo.append(oid)
        for i in range(0, len(todo), 25):  # max 25 per /getoffers call
            batch = todo[i:i + 25]
            self._throttle()
            data = http_json("POST", f"{self.BASE}/getoffers", self._headers(True), json.dumps(batch).encode())
            for offer in data or []:
                oid = offer.get("Id")
                if oid:
                    result[oid] = offer
                    self.cache.put(f"woot:offer:{oid}", offer)
        return result


# --------------------------------------------------------------------------- ebay

CONDITION_IDS = {
    "new": "1000|1500",
    "refurbished": "2000|2010|2020|2030|2500",
    "used": "1500|3000",
}


def condition_bucket(cond: str | None) -> str:
    c = (cond or "").lower()
    if "refurb" in c or "renewed" in c:
        return "refurbished"
    if "used" in c or "open box" in c or "pre-owned" in c or "scratch" in c:
        return "used"
    if "new" in c:
        return "new"
    return "unknown"


class Ebay:
    TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
    SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"

    def __init__(self, client_id: str, client_secret: str, marketplace: str, cache: Cache):
        self.cid, self.secret, self.mkt, self.cache = client_id, client_secret, marketplace, cache
        self._token, self._exp = None, 0.0
        self.calls = 0

    def _get_token(self) -> str:
        if self._token and time.time() < self._exp - 60:
            return self._token
        basic = base64.b64encode(f"{self.cid}:{self.secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "scope": "https://api.ebay.com/oauth/api_scope",
        }).encode()
        data = http_json("POST", self.TOKEN_URL, {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
        }, body)
        self._token, self._exp = data["access_token"], time.time() + int(data.get("expires_in", 7200))
        return self._token

    def search(self, q: str, bucket: str, min_price: float, limit: int) -> list[dict]:
        filters = ["buyingOptions:{FIXED_PRICE}", "itemLocationCountry:US", "deliveryCountry:US",
                   f"price:[{min_price:.0f}..],priceCurrency:USD"]
        if bucket in CONDITION_IDS:
            filters.append(f"conditionIds:{{{CONDITION_IDS[bucket]}}}")
        params = {"q": q, "filter": ",".join(filters), "limit": str(limit)}
        ck = "ebay:" + json.dumps(params, sort_keys=True)
        hit = self.cache.get(ck)
        if hit is not None:
            return hit
        url = f"{self.SEARCH_URL}?{urllib.parse.urlencode(params)}"
        self.calls += 1
        data = http_json("GET", url, {
            "Authorization": f"Bearer {self._get_token()}",
            "X-EBAY-C-MARKETPLACE-ID": self.mkt,
            "Accept": "application/json",
        })
        items = data.get("itemSummaries", []) or []
        self.cache.put(ck, items)
        return items


# --------------------------------------------------------------------------- matching

STOP = {
    "refurbished", "renewed", "certified", "new", "open", "box", "used", "factory", "manufacturer",
    "with", "and", "for", "the", "in", "of", "a", "an", "your", "choice", "options", "multiple",
    "grade", "scratch", "dent", "sealed", "brand",
}
JUNK = re.compile(
    r"\b(for parts|parts only|not working|broken|as[- ]is|box only|empty box|manual only|"
    r"replacement|compatible with|fits|case for|cover for|skin for|charger for|"
    r"screen protector|lot of|wholesale|read description|untested)\b", re.I)


def build_query(title: str) -> str:
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", title)
    # Woot titles often append " - Your Choice" / " – 2 Pack" etc.; keep the product part
    parts = re.split(r"\s[-–—|]\s", t)
    if len(parts) > 1 and len(parts[0].split()) >= 3:
        t = parts[0]
    t = re.sub(r"[^\w\s\-./\"]", " ", t)
    words = [w for w in t.split() if w.lower().strip("-.") not in STOP]
    q = words[:8]
    # Never drop a model number (e.g. DCD996P2) just because the title is long
    for w in words[8:]:
        if re.search(r"\d", w) and re.search(r"[A-Za-z]", w) and len(w) >= 5:
            q.append(w)
    return " ".join(q).strip()


def key_tokens(query: str) -> tuple[set[str], set[str]]:
    toks = {w.lower().strip("-.\"") for w in query.split()}
    toks = {w for w in toks if len(w) >= 3 or any(c.isdigit() for c in w)}
    models = {w for w in toks if re.search(r"\d", w) and re.search(r"[a-z]", w)}
    return toks, models


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def is_match(query: str, listing_title: str, min_overlap: float = 0.6) -> bool:
    if JUNK.search(listing_title):
        return False
    toks, models = key_tokens(query)
    lt = norm(listing_title)
    lt_compact = lt.replace(" ", "")
    if models and not all(m.replace("-", "") in lt_compact for m in models):
        return False
    if not toks:
        return False
    hits = sum(1 for t in toks if norm(t).strip() and norm(t).replace(" ", "") in lt_compact)
    return hits / len(toks) >= min_overlap


# --------------------------------------------------------------------------- stats

def pctl(vals: list[float], p: float) -> float:
    s = sorted(vals)
    if not s:
        return 0.0
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def trim_outliers(vals: list[float]) -> list[float]:
    if len(vals) < 8:
        return vals
    q1, q3 = pctl(vals, 0.25), pctl(vals, 0.75)
    iqr = q3 - q1
    return [v for v in vals if q1 - 1.5 * iqr <= v <= q3 + 1.5 * iqr]


def listing_prices(item: dict) -> tuple[float, float] | None:
    try:
        price = float(item["price"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    ship = 0.0
    opts = item.get("shippingOptions") or []
    if opts:
        try:
            ship = float(opts[0].get("shippingCost", {}).get("value", 0) or 0)
        except (TypeError, ValueError):
            ship = 0.0
    return price, ship


# --------------------------------------------------------------------------- evaluation

@dataclass
class Result:
    woot_title: str
    condition: str
    woot_price: float
    woot_list_price: float
    query: str
    comps: int
    ebay_low: float
    ebay_median: float
    ebay_sell_est: float
    cost_in: float
    ebay_fees: float
    ship_out: float
    ebay_net: float
    local_sell_est: float
    local_net: float
    best_channel: str
    best_net: float
    roi: float
    confidence: str
    flagged: bool
    is_new: bool
    first_seen: str
    woot_url: str
    ebay_sold_url: str
    fb_url: str
    amazon_url: str
    sample_comp_url: str


def outbound_shipping(title: str, cfg: dict) -> float:
    t = title.lower()
    table = cfg["pricing"]["shipping_by_keyword"]
    hits = [float(v) for k, v in table.items() if re.search(rf"\b{re.escape(k.lower())}\b", t)]
    return max(hits) if hits else float(cfg["pricing"]["default_outbound_shipping"])


def links(query: str, cfg: dict, asin: str | None) -> tuple[str, str, str]:
    qq = urllib.parse.quote_plus(query)
    sold = f"https://www.ebay.com/sch/i.html?_nkw={qq}&LH_Sold=1&LH_Complete=1"
    fb = f"https://www.facebook.com/marketplace/{cfg['local']['fb_city_slug']}/search?query={qq}"
    amz = f"https://www.amazon.com/dp/{asin}" if asin else ""
    return sold, fb, amz


def evaluate(item: dict, ebay_items: list[dict], cfg: dict, cache: Cache) -> Result | None:
    P, F, W = cfg["pricing"], cfg["filters"], cfg["woot"]
    q = item["query"]
    matched, item_only = [], []
    sample = ""
    for li in ebay_items:
        if not is_match(q, li.get("title", "")):
            continue
        pr = listing_prices(li)
        if not pr:
            continue
        matched.append(pr[0] + pr[1])
        item_only.append(pr[0])
        sample = sample or li.get("itemWebUrl", "")
    matched = trim_outliers(matched)
    item_only = trim_outliers(item_only)
    sold_url, fb_url, amz_url = links(q, cfg, item.get("asin"))

    price = item["price"]
    cost = price * (1 + W["sales_tax_rate"]) + W["shipping_cost"]
    ship_out = outbound_shipping(item["title"], cfg)

    if matched:
        sell = pctl(matched, P["sell_percentile"]) * P["active_to_sold_haircut"]
        fees = sell * P["fee_rate"] + P["fee_fixed"]
        ebay_net = sell - fees - ship_out - cost
        local_sell = pctl(item_only, 0.5) * P["local_price_factor"]
        local_net = local_sell - cost
        low, med = min(matched), pctl(matched, 0.5)
    else:
        sell = fees = ebay_net = local_sell = local_net = low = med = 0.0

    best_channel, best_net = ("eBay", ebay_net) if ebay_net >= local_net else ("Local/FB", local_net)
    roi = best_net / cost if cost else 0.0

    n = len(matched)
    spread = (pctl(matched, 0.75) - pctl(matched, 0.25)) / med if n and med else 9
    if n >= 15 and spread < 0.35:
        conf = "High"
    elif n >= cfg["ebay"]["min_comps"] and spread < 0.6:
        conf = "Med"
    else:
        conf = "Low"

    flagged = (n >= cfg["ebay"]["min_comps"] and best_net >= F["min_profit"] and roi >= F["min_roi"])
    seen_key = f"{item['title']}|{item['condition']}|{price:.2f}"
    first, is_new = cache.first_seen(seen_key) if flagged else ("", False)

    r2 = lambda x: round(x, 2)
    return Result(
        woot_title=item["title"], condition=item["condition"], woot_price=r2(price),
        woot_list_price=r2(item.get("list_price") or 0), query=q, comps=n,
        ebay_low=r2(low), ebay_median=r2(med), ebay_sell_est=r2(sell), cost_in=r2(cost),
        ebay_fees=r2(fees), ship_out=r2(ship_out), ebay_net=r2(ebay_net),
        local_sell_est=r2(local_sell), local_net=r2(local_net), best_channel=best_channel,
        best_net=r2(best_net), roi=round(roi, 3), confidence=conf, flagged=flagged,
        is_new=is_new, first_seen=first, woot_url=item["url"], ebay_sold_url=sold_url,
        fb_url=fb_url, amazon_url=amz_url, sample_comp_url=sample,
    )


# --------------------------------------------------------------------------- woot -> items

def expand_items(feed: list[dict], offers: dict[str, dict], cfg: dict) -> list[dict]:
    """Flatten offers into one row per distinct (title, condition, price) item."""
    W = cfg["woot"]
    out, seen = [], set()
    for f in feed:
        offer = offers.get(f.get("OfferId"))
        base_title = (offer or {}).get("Title") or f.get("Title", "")
        url = (offer or {}).get("Url") or f.get("Url", "")
        items = (offer or {}).get("Items") or []
        if not items:  # fall back to feed-level min price
            sp = (f.get("SalePrice") or {}).get("Minimum")
            lp = (f.get("ListPrice") or {}).get("Minimum")
            items = [{"Title": base_title, "SalePrice": sp, "ListPrice": lp,
                      "Attributes": [{"Key": "Condition", "Value": f.get("Condition", "")}]}]
        for it in items:
            try:
                price = float(it.get("SalePrice") or 0)
            except (TypeError, ValueError):
                continue
            if not (W["min_price"] <= price <= W["max_price"]):
                continue
            attrs = {a.get("Key"): a.get("Value") for a in it.get("Attributes") or []}
            cond = attrs.get("Condition") or f.get("Condition") or ""
            title = base_title
            key = (title.lower(), condition_bucket(cond), round(price, 2))
            if key in seen:  # color/size variants at same price -> one comp search
                continue
            seen.add(key)
            out.append({
                "title": title, "condition": cond, "bucket": condition_bucket(cond),
                "price": price, "list_price": it.get("ListPrice"), "asin": it.get("Asin"),
                "url": url, "query": build_query(title),
            })
    return out


def prefilter_feed(feed: list[dict], cfg: dict) -> list[dict]:
    skip = {c.lower() for c in cfg["woot"]["skip_categories"]}
    out, ids = [], set()
    for f in feed:
        if f.get("IsSoldOut") or f.get("IsAvailableOnMobileAppOnly"):
            continue
        cats = {str(c).lower() for c in (f.get("Categories") or [])}
        site = str(f.get("Site", "")).lower()
        if cats & skip or any(s in site for s in skip):
            continue
        oid = f.get("OfferId")
        if oid in ids:
            continue
        ids.add(oid)
        out.append(f)
    return out


# --------------------------------------------------------------------------- report

def money(x: float) -> str:
    return f"-${abs(x):,.0f}" if x < 0 else f"${x:,.0f}"


def write_reports(results: list[Result], outdir: Path, stats: dict) -> tuple[Path, Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    csv_path = outdir / f"woot_flips_{stamp}.csv"
    html_path = outdir / f"woot_flips_{stamp}.html"

    with open(csv_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(asdict(results[0]).keys()) if results else ["none"])
        w.writeheader()
        for r in results:
            w.writerow(asdict(r))

    flagged = [r for r in results if r.flagged]
    watch = [r for r in results if not r.flagged and r.comps and r.best_net > 0][:25]
    e = html.escape

    def card(r: Result) -> str:
        badge = '<span class="new">NEW</span>' if r.is_new else ""
        amz = f'<a href="{e(r.amazon_url)}">Amazon</a>' if r.amazon_url else ""
        comp = f'<a href="{e(r.sample_comp_url)}">Sample comp</a>' if r.sample_comp_url else ""
        return f"""
<div class="card {'hot' if r.flagged else ''}">
  <div class="top">{badge}<span class="conf c{r.confidence}">{r.confidence}</span>
    <span class="cond">{e(r.condition or '?')}</span></div>
  <h3>{e(r.woot_title)}</h3>
  <div class="big">{money(r.best_net)} <small>net via {r.best_channel} · {r.roi:.0%} ROI</small></div>
  <table>
    <tr><td>Woot price</td><td>${r.woot_price:,.2f}</td><td>All-in cost</td><td>${r.cost_in:,.2f}</td></tr>
    <tr><td>eBay comps</td><td>{r.comps} (low ${r.ebay_low:,.0f}, med ${r.ebay_median:,.0f})</td>
        <td>eBay sell est.</td><td>${r.ebay_sell_est:,.0f}</td></tr>
    <tr><td>eBay fees + ship</td><td>${r.ebay_fees:,.0f} + ${r.ship_out:,.0f}</td>
        <td>eBay net</td><td>{money(r.ebay_net)}</td></tr>
    <tr><td>Local sell est.</td><td>${r.local_sell_est:,.0f}</td><td>Local net</td><td>{money(r.local_net)}</td></tr>
  </table>
  <div class="links"><a href="{e(r.woot_url)}">Woot</a><a href="{e(r.ebay_sold_url)}">eBay SOLD</a>
    <a href="{e(r.fb_url)}">FB Marketplace</a>{amz}{comp}</div>
  <div class="q">query: {e(r.query)}</div>
</div>"""

    body = "".join(card(r) for r in flagged) or "<p>No items cleared your profit/ROI thresholds this run.</p>"
    watch_html = "".join(card(r) for r in watch)
    doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Woot flips {stamp}</title>
<style>
body{{font:15px/1.4 -apple-system,system-ui,sans-serif;margin:0;padding:12px;background:#f4f4f2;color:#1b1b1b}}
h1{{font-size:20px;margin:4px 0}} h2{{font-size:16px;margin:20px 0 8px}}
.meta{{color:#666;font-size:13px}}
.card{{background:#fff;border-radius:10px;padding:12px;margin:10px 0;border-left:5px solid #bbb}}
.card.hot{{border-left-color:#1a7f37}}
.card h3{{font-size:15px;margin:6px 0}}
.big{{font-size:22px;font-weight:700;color:#1a7f37}} .big small{{font-size:13px;color:#555;font-weight:400}}
table{{width:100%;font-size:13px;border-collapse:collapse;margin:6px 0}} td{{padding:2px 4px}}
td:nth-child(odd){{color:#666}}
.links a{{display:inline-block;margin:4px 6px 0 0;padding:5px 9px;background:#eef;border-radius:6px;
  text-decoration:none;color:#224;font-size:13px}}
.q{{color:#999;font-size:11px;margin-top:6px}}
.top span{{font-size:11px;padding:2px 6px;border-radius:4px;margin-right:4px;background:#eee}}
.new{{background:#ffd54a!important;font-weight:700}}
.cHigh{{background:#c8f0d0!important}} .cMed{{background:#fff1c2!important}} .cLow{{background:#f6d0d0!important}}
</style></head><body>
<h1>Woot → resale opportunities</h1>
<div class="meta">{stamp.replace('_', ' ')} · {stats['offers']} offers · {stats['items']} items comped ·
 {len(flagged)} flagged · Woot API calls {stats['woot_calls']} · eBay calls {stats['ebay_calls']}</div>
<div class="meta">Comps are <b>active</b> eBay listings, discounted toward sold prices. Tap <b>eBay SOLD</b>
 to confirm before buying. Local estimates are derived from eBay data, not FB.</div>
<h2>Flagged ({len(flagged)})</h2>{body}
<h2>Watchlist — positive but below thresholds</h2>{watch_html or '<p>None.</p>'}
</body></html>"""
    html_path.write_text(doc)
    return html_path, csv_path


# --------------------------------------------------------------------------- demo stubs

class FakeWoot:
    calls = 0

    def __init__(self, fx): self.fx = fx
    def feed(self, name, max_pages): return self.fx["feed"]
    def offers(self, ids): return {o["Id"]: o for o in self.fx["offers"] if o["Id"] in ids}


class FakeEbay:
    calls = 0

    def __init__(self, fx): self.fx = fx

    def search(self, q, bucket, min_price, limit):
        self.calls += 1
        for key, items in self.fx["ebay"].items():
            if key.lower() in q.lower():
                return [i for i in items if float(i["price"]["value"]) >= min_price]
        return []


# --------------------------------------------------------------------------- main

def run(args) -> int:
    if args.config:
        cfg_path = Path(args.config)
    else:
        cfg_path = HERE / "config.toml"
        if not cfg_path.exists():
            cfg_path = HERE / "config.example.toml"
    cfg = load_config(cfg_path)
    cache = Cache(HERE / cfg["cache"]["path"] if not args.demo else Path(":memory:"), cfg["cache"]["ttl_hours"])

    if args.demo:
        fx = json.loads((HERE / "fixtures" / "demo.json").read_text())
        woot, ebay = FakeWoot(fx), FakeEbay(fx)
    else:
        missing = [k for k in ("WOOT_API_KEY", "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET") if not secret(cfg, k)]
        if missing:
            print(f"Missing credentials: {', '.join(missing)} (set env vars or [secrets] in config.toml)")
            return 2
        woot = Woot(secret(cfg, "WOOT_API_KEY"), cache)
        ebay = Ebay(secret(cfg, "EBAY_CLIENT_ID"), secret(cfg, "EBAY_CLIENT_SECRET"),
                    cfg["ebay"]["marketplace"], cache)

    feed = []
    for name in cfg["woot"]["feeds"]:
        print(f"[woot] feed {name} ...", flush=True)
        feed.extend(woot.feed(name, cfg["woot"]["max_pages"]))
    feed = prefilter_feed(feed, cfg)
    print(f"[woot] {len(feed)} live offers after prefilter")

    offers = woot.offers([f["OfferId"] for f in feed if f.get("OfferId")])
    items = expand_items(feed, offers, cfg)
    # Best Woot discounts first, so a query cap spends calls on the likeliest wins
    items.sort(key=lambda i: (float(i["list_price"] or 0) - i["price"]) / max(i["price"], 1), reverse=True)
    cap = min(args.limit or 10**9, cfg["ebay"]["max_queries"])
    items = items[:cap]
    print(f"[ebay] comping {len(items)} items ...", flush=True)

    results = []
    for n, it in enumerate(items, 1):
        if not it["query"]:
            continue
        try:
            listings = ebay.search(it["query"], it["bucket"], it["price"] * 0.4, cfg["ebay"]["results_per_query"])
        except RuntimeError as e:
            print(f"  ! eBay error on '{it['query']}': {e}")
            continue
        r = evaluate(it, listings, cfg, cache)
        if r:
            results.append(r)
        if n % 25 == 0:
            print(f"  {n}/{len(items)}", flush=True)

    results.sort(key=lambda r: (r.flagged, r.best_net), reverse=True)
    stats = {"offers": len(feed), "items": len(items), "woot_calls": woot.calls, "ebay_calls": ebay.calls}
    html_path, csv_path = write_reports(results, Path(args.out), stats)

    flagged = [r for r in results if r.flagged]
    print(f"\n{len(flagged)} flagged of {len(results)} evaluated")
    for r in flagged[:10]:
        print(f"  {money(r.best_net):>7} {r.roi:>5.0%} {r.best_channel:<8} {r.confidence:<4} {r.woot_title[:60]}")
    print(f"\nReport: {html_path}\nCSV:    {csv_path}")

    if args.site_dir:  # fixed names for GitHub Pages
        site = Path(args.site_dir)
        site.mkdir(parents=True, exist_ok=True)
        (site / "woot.html").write_text(html_path.read_text())
        (site / "woot.csv").write_text(csv_path.read_text())

    new = [r for r in flagged if r.is_new]
    if new and not args.demo:
        lines = [f"{money(r.best_net)} ({r.roi:.0%}, {r.best_channel}) {r.woot_title[:55]}" for r in new[:6]]
        notify(f"Woot: {len(new)} new flip{'s' if len(new) > 1 else ''}", "\n".join(lines),
               click=args.report_url or new[0].woot_url)
    return 0


def main():
    ap = argparse.ArgumentParser(description="Find Woot deals worth reselling.")
    ap.add_argument("--config", help="path to config.toml (default: next to script)")
    ap.add_argument("--out", default=str(HERE / "reports"), help="output folder")
    ap.add_argument("--limit", type=int, help="max Woot items to comp this run")
    ap.add_argument("--demo", action="store_true", help="offline run on fixtures/demo.json")
    ap.add_argument("--site-dir", help="also write woot.html / woot.csv here (for GitHub Pages)")
    ap.add_argument("--report-url", help="link opened when you tap the phone notification")
    sys.exit(run(ap.parse_args()))


if __name__ == "__main__":
    main()
