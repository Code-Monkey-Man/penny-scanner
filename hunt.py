#!/usr/bin/env python3
"""
hunt.py: turn penny-list.json into a ranked hunt list for your home stores.

For every Home Depot and Lowe's penny-list item it scores:
  likelihood  0-100: how likely the item is still pennied at stores near you. Based on
              reports in your state and neighboring states, how many states/stores report it,
              how recently it was seen, and report volume (receipt-verified items get a bump).
  value       what it nets if resold. eBay comps (new condition) minus fees and shipping,
              or a local cash sale, whichever is higher. Without eBay keys it falls back to
              a rough retail-based estimate, marked as such.
  best bet    likelihood^2 x value (retail-only estimates discounted), the default sort.

Writes hunt.html (phone page) and hunt.json, and pushes a phone notification (ntfy) for items
that became Hot since the last run.

  python3 hunt.py                 # uses EBAY_CLIENT_ID / EBAY_CLIENT_SECRET if set
  python3 hunt.py --no-comps      # likelihood only, no eBay calls
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import math
import re
import sys
import time
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "woot"))
from woot_flip import (Cache, Ebay, build_query, is_match, listing_prices, load_config,  # noqa: E402
                       notify, outbound_shipping, pctl, secret, trim_outliers)
from region import REGION  # noqa: E402

HOME = REGION["state"]
NEIGHBORS = set(REGION.get("neighbor_states", []))
STORE_NAMES = {"home_depot": "Home Depot", "lowes": "Lowe's"}
STATE_IN_STORE = re.compile(r",\s*([A-Z]{2})\b")


def recency(days: int | None) -> float:
    if days is None:
        return 0.1
    for limit, val in ((2, 1.0), (7, 0.75), (14, 0.45), (30, 0.2)):
        if days <= limit:
            return val
    return 0.05


def likelihood(it: dict, today: dt.date) -> tuple[float, str]:
    last = it.get("last_seen") or it.get("listed")
    days = (today - dt.date.fromisoformat(last[:10])).days if last else None
    reports = it.get("reports") or 0
    vol = min(math.log10(reports + 1) / 2.5, 1.0)
    bonus, why = 0.0, []

    if it["store"] == "home_depot":
        states = it.get("states") or {}
        breadth = min(len(states) / 25, 1.0)
        home_n = states.get(HOME, 0)
        near = sorted(s for s in states if s in NEIGHBORS)
        local = 1.0 if home_n else (0.6 if near else 0.0)
        if home_n:
            why.append(f"{home_n} {HOME} report{'s' if home_n > 1 else ''}")
        elif near:
            why.append("nearby: " + ",".join(near[:4]))
        why.append(f"{len(states)} states")
        if it.get("receipt_verified"):
            bonus = 5.0
            why.append("receipt-verified")
    else:  # lowes
        stores = it.get("all_stores") or []
        breadth = min(len(stores) / 150, 1.0)
        st = {m.group(1) for s in stores for m in [STATE_IN_STORE.search(s)] if m}
        if it.get("home_stores") or it.get("home_reports"):
            local = 1.0
            why.append(f"{len(it.get('home_stores') or [])} stores near you")
        elif HOME in st:
            local = 0.8
            why.append(f"seen in {HOME}")
        elif st & NEIGHBORS:
            local = 0.5
            why.append("nearby: " + ",".join(sorted(st & NEIGHBORS)[:4]))
        else:
            local = 0.0
        why.append(f"{len(stores)} stores")

    why.append(f"seen {days}d ago" if days is not None else "date unknown")
    why.append(f"{reports} reports")
    score = 100 * (0.30 * breadth + 0.25 * local + 0.30 * recency(days) + 0.15 * vol) + bonus
    return round(min(score, 100), 1), " · ".join(why)


def tier(score: float) -> str:
    return "Hot" if score >= 70 else "Warm" if score >= 50 else "Cold"


def bet(score: float, v: dict) -> float:
    """Expected-value style rank. Likelihood is squared so a likely $60 item beats an unlikely $300
    one, and retail-only estimates are discounted against real comps."""
    conf = 1.0 if v["basis"] == "eBay comps" else 0.7
    return round((score / 100) ** 2 * max(v["value"] or 0, 0) * conf, 2)


def comp_query(it: dict) -> str:
    brand = (it.get("brand") or "").strip()
    model = (it.get("model") or "").strip()
    if model and len(model) >= 4 and re.search(r"\d", model):
        lead = brand or " ".join(build_query(it["name"]).split()[:2])
        return f"{lead} {model}".strip()
    q = build_query(it["name"])
    if brand and brand.lower() not in q.lower():
        q = f"{brand} {q}"
    return q


def value_item(it: dict, q: str, ebay: Ebay | None, cfg: dict) -> dict:
    P = cfg["pricing"]
    retail = it.get("retail")
    cost = (it.get("price") or 0.01) * (1 + cfg["woot"]["sales_tax_rate"])
    out = {"comps": 0, "ebay_net": None, "local_net": None, "value": None, "channel": None,
           "basis": None, "median": None}
    if ebay:
        min_price = max(5.0, 0.15 * retail) if retail else 5.0
        try:
            listings = ebay.search(q, "new", min_price, cfg["ebay"]["results_per_query"])
        except RuntimeError as e:
            print(f"  ! eBay error on '{q}': {e}")
            listings = []
        totals, items = [], []
        for li in listings:
            if not is_match(q, li.get("title", "")):
                continue
            pr = listing_prices(li)
            if pr:
                totals.append(pr[0] + pr[1])
                items.append(pr[0])
        totals, items = trim_outliers(totals), trim_outliers(items)
        out["comps"] = len(totals)
        if len(totals) >= 3:
            sell = pctl(totals, P["sell_percentile"]) * P["active_to_sold_haircut"]
            fees = sell * P["fee_rate"] + P["fee_fixed"]
            ebay_net = sell - fees - outbound_shipping(it["name"], cfg) - cost
            local_net = pctl(items, 0.5) * P["local_price_factor"] - cost
            out.update(ebay_net=round(ebay_net, 2), local_net=round(local_net, 2),
                       median=round(pctl(totals, 0.5), 2), basis="eBay comps")
            out["value"], out["channel"] = max((ebay_net, "eBay"), (local_net, "Local/FB"))
            out["value"] = round(out["value"], 2)
    if out["value"] is None and retail:
        # No usable comps: rough local resale at ~40% of retail. Shown as an estimate.
        out.update(value=round(retail * 0.40 - cost, 2), channel="Local/FB", basis="40% of retail (no comps)")
    return out


def links(it: dict, q: str) -> dict:
    qq = urllib.parse.quote_plus(q)
    if it["store"] == "home_depot":
        inet = it.get("internet_number")
        store_url = (f"https://www.homedepot.com/p/{inet}" if inet else
                     f"https://www.homedepot.com/s/{urllib.parse.quote(it.get('sku') or q)}")
    else:
        store_url = f"https://www.lowes.com/search?searchTerm={urllib.parse.quote(it.get('sku') or q)}"
    return {"store_url": store_url,
            "ebay_sold": f"https://www.ebay.com/sch/i.html?_nkw={qq}&LH_Sold=1&LH_Complete=1",
            "fb": f"https://www.facebook.com/marketplace/nashville/search?query={qq}"}


def render(rows: list[dict], meta: dict) -> str:
    data = json.dumps(rows, separators=(",", ":")).replace("</", "<\\/")
    m = json.dumps(meta).replace("</", "<\\/")
    return """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Penny Hunt List</title><link rel="icon" href="icon-192.png">
<style>
:root{--or:#f96302;--bg:#f5f5f3;--card:#fff;--mut:#666}
body{margin:0;font:15px/1.4 -apple-system,system-ui,sans-serif;background:var(--bg);color:#1b1b1b}
header{background:var(--or);color:#fff;padding:calc(env(safe-area-inset-top) + 10px) 14px 10px;position:sticky;top:0;z-index:5}
header h1{font-size:18px;margin:0} header small{opacity:.9;font-size:12px}
header a{color:#fff}
.bar{display:flex;gap:6px;flex-wrap:wrap;padding:8px 12px;background:#fff;border-bottom:1px solid #ddd;position:sticky;top:58px;z-index:4}
.bar button{border:1px solid #ccc;background:#fff;border-radius:16px;padding:5px 11px;font-size:13px}
.bar button.on{background:#222;color:#fff;border-color:#222}
main{padding:8px 12px 40px}
.card{background:var(--card);border-radius:10px;padding:11px 12px;margin:9px 0;border-left:5px solid #bbb}
.card.Hot{border-left-color:#d62d20}.card.Warm{border-left-color:#f0a500}
.top{display:flex;gap:5px;align-items:center;flex-wrap:wrap;font-size:11px}
.tag{padding:2px 7px;border-radius:4px;background:#eee}
.t-Hot{background:#d62d20;color:#fff}.t-Warm{background:#f0a500;color:#fff}
.st-home_depot{background:#f96302;color:#fff}.st-lowes{background:#004990;color:#fff}
.new{background:#ffd54a;font-weight:700}
h3{font-size:15px;margin:6px 0 3px}
.why{color:var(--mut);font-size:12px}
.nums{display:flex;gap:14px;margin:7px 0 2px;font-size:13px;flex-wrap:wrap}
.nums b{font-size:17px}
.val{color:#1a7f37}.neg{color:#b42318}
.ids{font:12px ui-monospace,Menlo,monospace;color:#333;margin-top:4px}
.links a{display:inline-block;margin:6px 6px 0 0;padding:5px 9px;background:#eef;border-radius:6px;text-decoration:none;color:#224;font-size:13px}
.note{font-size:12px;color:var(--mut);margin:10px 2px}
</style></head><body>
<header><h1>🎯 Penny Hunt List</h1><small id="meta"></small></header>
<div class="bar" id="bar">
 <button data-sort="bet" class="on">Best bet</button><button data-sort="score">Most likely</button><button data-sort="value">Most value</button>
 <button data-store="all" class="on">All</button><button data-store="home_depot">HD</button><button data-store="lowes">Lowe's</button>
 <button data-hot="1">Hot only</button>
</div>
<main id="list"></main>
<p class="note" style="padding:0 12px">Likelihood comes from crowdsourced reports (PennyCentral, Penny Recon, ClearanceFinds), not store data.
A listed item is a lead. Scan the barcode at self-checkout to see the real price. Penny items aren't officially for sale,
so staff can decline. Value: eBay active-listing comps after fees and shipping, or a local sale, whichever is higher.
Tap <b>eBay SOLD</b> to confirm. <a href="./">← Penny Scanner</a></p>
<script>
const ROWS = """ + data + """;
const META = """ + m + """;
const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const $m = v => v == null ? '—' : (v < 0 ? '-$' : '$') + Math.abs(v).toFixed(0);
const st = {sort:'bet', store:'all', hot:false};
document.getElementById('meta').textContent =
  `${META.area} · list ${META.list_updated} · ranked ${META.generated} · ${META.with_comps} with eBay comps`;
function draw(){
  let r = ROWS.filter(x => (st.store==='all' || x.store===st.store) && (!st.hot || x.tier==='Hot'));
  const k = {bet:x=>x.bet, score:x=>x.score, value:x=>x.value ?? -1e9}[st.sort];
  r.sort((a,b)=>k(b)-k(a));
  document.getElementById('list').innerHTML = r.slice(0,300).map(x => `
  <div class="card ${x.tier}">
   <div class="top"><span class="tag t-${x.tier}">${x.tier} ${x.score}</span>
    <span class="tag st-${x.store}">${x.store==='home_depot'?'Home Depot':"Lowe's"}</span>
    ${x.is_new?'<span class="tag new">NEW</span>':''}${x.brand?`<span class="tag">${esc(x.brand)}</span>`:''}</div>
   <h3>${esc(x.name)}</h3>
   <div class="why">${esc(x.why)}</div>
   <div class="nums">
    <span>Value <b class="${(x.value??0)>=0?'val':'neg'}">${$m(x.value)}</b> ${x.channel?'via '+x.channel:''}</span>
    <span>Retail ${x.retail!=null?'$'+x.retail:'—'}</span>
    ${x.comps?`<span>${x.comps} comps · med ${$m(x.median)}</span>`:''}
   </div>
   ${x.basis && x.basis!=='eBay comps'?`<div class="why">value basis: ${esc(x.basis)}</div>`:''}
   <div class="ids">SKU ${esc(x.sku_display||x.sku||'—')}${x.model?' · Model '+esc(x.model):''}${x.upc?' · UPC '+esc(x.upc):''}</div>
   <div class="links"><a href="${x.links.store_url}">Store page</a><a href="${x.links.ebay_sold}">eBay SOLD</a><a href="${x.links.fb}">FB</a></div>
  </div>`).join('') || '<p>No items match.</p>';
}
document.getElementById('bar').addEventListener('click', e => {
  const b = e.target.closest('button'); if(!b) return;
  if (b.dataset.sort){ st.sort=b.dataset.sort; document.querySelectorAll('[data-sort]').forEach(x=>x.classList.toggle('on',x===b)); }
  if (b.dataset.store){ st.store=b.dataset.store; document.querySelectorAll('[data-store]').forEach(x=>x.classList.toggle('on',x===b)); }
  if (b.dataset.hot){ st.hot=!st.hot; b.classList.toggle('on',st.hot); }
  draw();
});
draw();
</script></body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--list", default=str(HERE / "penny-list.json"))
    ap.add_argument("--config", default=str(HERE / "woot" / "config.toml"))
    ap.add_argument("--no-comps", action="store_true", help="skip eBay valuation")
    ap.add_argument("--min-score-for-comps", type=float, default=35.0)
    ap.add_argument("--page-url", default="", help="link opened from the phone notification")
    a = ap.parse_args()

    cfg = load_config(Path(a.config))
    data = json.loads(Path(a.list).read_text())
    today = dt.date.today()
    items = [i for i in data["items"] if i.get("store") in STORE_NAMES]

    prev_hot = set()
    out_json = HERE / "hunt.json"
    if out_json.exists():
        try:
            prev_hot = {r["id"] for r in json.loads(out_json.read_text())["items"] if r["tier"] == "Hot"}
        except (ValueError, KeyError):
            pass

    ebay = None
    if not a.no_comps and secret(cfg, "EBAY_CLIENT_ID") and secret(cfg, "EBAY_CLIENT_SECRET"):
        cache = Cache(HERE / "hunt_cache.sqlite", ttl_hours=72)  # resale prices move slowly
        ebay = Ebay(secret(cfg, "EBAY_CLIENT_ID"), secret(cfg, "EBAY_CLIENT_SECRET"),
                    cfg["ebay"]["marketplace"], cache)
    print(f"[hunt] {len(items)} HD/Lowe's items · eBay comps {'ON' if ebay else 'OFF'}")

    scored = []
    for it in items:
        score, why = likelihood(it, today)
        scored.append((score, why, it))
    scored.sort(key=lambda t: t[0], reverse=True)

    rows, budget = [], cfg["ebay"]["max_queries"]
    for score, why, it in scored:
        q = comp_query(it)
        use_ebay = ebay if (ebay and score >= a.min_score_for_comps and budget > 0
                            and (it.get("retail") or 99) >= 10) else None
        if use_ebay:
            budget -= 1
        v = value_item(it, q, use_ebay, cfg)
        if use_ebay:
            time.sleep(0.15)
        iid = it.get("id") or f"{it['store']}:{it.get('sku')}"
        t = tier(score)
        rows.append({
            "id": iid, "store": it["store"], "name": it["name"], "brand": it.get("brand"),
            "sku": it.get("sku"), "sku_display": it.get("sku_display"), "model": it.get("model"),
            "upc": it.get("upc"), "retail": it.get("retail"), "score": score, "tier": t, "why": why,
            "query": q, **v, "bet": bet(score, v),
            "is_new": t == "Hot" and iid not in prev_hot, "links": links(it, q),
        })

    meta = {"area": REGION["short"] + ", " + HOME, "list_updated": data.get("updated"),
            "generated": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "with_comps": sum(1 for r in rows if r["basis"] == "eBay comps")}
    out_json.write_text(json.dumps({"meta": meta, "items": rows}, indent=1))
    (HERE / "hunt.html").write_text(render(rows, meta))

    hot = [r for r in rows if r["tier"] == "Hot"]
    print(f"[hunt] {len(hot)} Hot · {sum(r['tier'] == 'Warm' for r in rows)} Warm · {meta['with_comps']} valued via eBay")
    for r in sorted(rows, key=lambda r: r["bet"], reverse=True)[:8]:
        print(f"  {r['tier']:<4} {r['score']:>5} {('$%.0f' % r['value']) if r['value'] is not None else '   —':>6} "
              f"{STORE_NAMES[r['store']]:<10} {r['name'][:55]}")

    new_hot = sorted((r for r in rows if r["is_new"] and (r["value"] or 0) >= cfg["filters"]["min_profit"]),
                     key=lambda r: r["bet"], reverse=True)
    if new_hot and prev_hot:  # skip the very first run so you don't get 50 alerts at once
        lines = [f"${r['value']:.0f} · {STORE_NAMES[r['store']]} · {r['name'][:50]}" for r in new_hot[:6]]
        notify(f"Penny hunt: {len(new_hot)} new Hot item{'s' if len(new_hot) > 1 else ''}",
               "\n".join(lines), click=a.page_url or None, tags="dart")
    return 0


if __name__ == "__main__":
    sys.exit(main())
