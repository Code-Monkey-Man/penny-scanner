# Penny Scanner (Clarksville / Nashville, TN: Home Depot, Lowe's, Dollar General, Walmart)

Adapted from jbozarth206/penny-scanner (originally Clermont, FL). The location lives in one file: region.py.

## Hunt list + Woot flips (added Nov 2026)

Two pages sit beside the scanner, linked from its top bar:

- **hunt.html** ranks every Home Depot and Lowe's penny-list item for your area.
  - **Likelihood (0–100)** is built from TN reports, reports in neighboring states (region.py `neighbor_states`), how many states/stores report it, how recently it was seen, report volume, and receipt verification. Hot ≥ 70, Warm ≥ 50.
  - **Value** is eBay active-listing comps after fees and shipping, or a local sale, whichever is higher. Without eBay keys it uses ~40% of retail and says so.
  - **Best bet** is likelihood² × value, the default sort.
  - It is rebuilt by the morning "Refresh penny list" run (`hunt.py`).
- **deals/woot.html** is the daily Woot → resale scan (`woot/woot_flip.py`, workflow "Woot flips", 7:13 AM Central). See woot/README.md.

### Secrets (Settings → Secrets and variables → Actions → New repository secret)
| Secret | Used by | Where to get it |
|---|---|---|
| `EBAY_CLIENT_ID`, `EBAY_CLIENT_SECRET` | hunt + woot | developer.ebay.com → Application Keys → Production (App ID / Cert ID) |
| `WOOT_API_KEY` | woot | Ask in the "Request Developer API Key" thread on forums.woot.com |
| `NTFY_TOPIC` | phone alerts | Install the free **ntfy** app and subscribe to a long random topic name (e.g. `zk-deals-8f3k29xq`). Put the same name in the secret. |

Alerts: the hunt run notifies you when items newly turn Hot with value ≥ $25. The Woot run notifies you about newly flagged flips. Tapping an alert opens the page.
The hunt page works with no secrets at all, using likelihood and retail-based value. The Woot workflow skips itself until its keys exist.

Limits: penny (.01) prices never show online, and Home Depot's site blocks automated store-price lookups from cloud servers.
The hunt list ranks crowdsourced reports and does not read Home Depot's inventory. Scan the item at self-checkout to confirm the price.

## Setup (about 10 minutes, free)
1. Create a new GitHub repository (public, e.g. `penny-scanner`) and upload every file and folder in this zip,
   including the hidden `.github` folder. Easiest: GitHub Desktop or `git push`; the web uploader skips `.github`.
2. Settings -> Pages -> "Deploy from a branch" -> branch `main`, folder `/ (root)` -> Save.
3. Actions tab -> enable workflows -> "Refresh penny list" -> Run workflow (pulls today's lists).
4. Open https://<your-username>.github.io/penny-scanner/ on your phone and use "Add to Home Screen".
   The list refreshes itself every morning after that.

Static PWA: scan a barcode (or take a photo, or type a UPC / SKU / Lowe's item # / Walmart item ID) and check it
against crowdsourced penny lists (Home Depot, Lowe's, Dollar General) and Walmart clearance leads.
Every item has a store, a date and its source URL(s). Nothing is invented: codes and prices come from the sources.

Files: index.html, app.js, vendor/zxing.min.js (@zxing/library 0.21.3, vendored), penny-list.json (schema 2, all stores),
manifest.webmanifest, sw.js (offline cache), icons, update_list.py (refresh, Home Depot fetcher + combiner),
store_fetchers.py (Dollar General, Lowe's, Walmart fetchers), deploy.sh, raw/ (cached source pages, not deployed).

Sources
- Home Depot: PennyCentral, Penny Recon (+ weekly receipts report)
- Dollar General: Penny General (pennygeneral.net, this penny week's lists), PennyPinchinMom, GlitchNDealz
- Lowe's: ClearanceFinds monthly Lowe's penny list (item #, observed 1-6 cent prices, dated store rows)
- Walmart: GlitchNDealz dated in-store clearance posts (item ID + clearance and comparison price). No free source
  publishes store-level Walmart hidden clearance for this area; RebelSavings (free, but JS + Turnstile) and the
  Walmart app's "Check a price" scanner are the manual options.

Weekly refresh:  ./deploy.sh [--report-week <latest Monday with a Penny Recon report>]
Local test:      python3 -m http.server 8000   (camera works on http://localhost only; iPhone needs HTTPS)
Tests:           /home/box/pwvenv/bin/python /workspace/penny-test/test.py   (URL=... to test the live site)

Bump CACHE in sw.js whenever app files (index.html, app.js, sw.js, manifest) change; penny-list.json is network-first.
