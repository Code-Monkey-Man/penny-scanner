# woot_flip

Finds Woot deals that can be resold at a profit. It pulls live offers from Woot's official API, compares each one against eBay listings in the same condition, subtracts fees, tax and shipping, and gives you a ranked report you can read on your phone.

## Setup (one time)

1. **Python 3.11+.** You don't need to install any packages.
2. **Woot API key.** Post a request in the "Request Developer API Key" thread on forums.woot.com. They send the key by private message.
3. **eBay keys.** Sign up at developer.ebay.com, then go to Application Keys and create a **Production** keyset. You need the App ID (client ID) and the Cert ID (client secret).
4. Set your keys:
   ```bash
   export WOOT_API_KEY=...
   export EBAY_CLIENT_ID=...
   export EBAY_CLIENT_SECRET=...
   ```
5. `cp config.example.toml config.toml` and adjust the settings. Set `shipping_cost = 0` if Prime is linked to Woot, and check `fee_rate` against your eBay categories.

## Run

```bash
python woot_flip.py --demo        # offline test on fake data, needs no keys
python woot_flip.py               # live run
python woot_flip.py --limit 50    # comp only the 50 best Woot discounts
```

The report goes to `reports/woot_flips_<date>.html` (phone-friendly) and a matching `.csv`.

**Daily schedule** (cron, 7 AM, after Woot's daily turnover):
```
0 7 * * * cd /path/to/woot_flip && /usr/bin/python3 woot_flip.py >> run.log 2>&1
```

## How an item is scored

```
cost_in   = woot_price × (1 + sales tax) + woot shipping
ebay_sell = 30th percentile of matching active listings (price + buyer shipping) × 0.90
ebay_net  = ebay_sell − (fee_rate × ebay_sell + $0.40) − outbound label − cost_in
local_net = median eBay item price × 0.75 − cost_in          (no fees, no shipping)
```

An item is **flagged** when the better channel nets ≥ `min_profit` and ROI ≥ `min_roi`, with at least `min_comps` matching listings. Confidence (High/Med/Low) comes from the number of comps and how tightly their prices cluster. Items flagged for the first time are marked **NEW**.

Matching rules: model numbers in the Woot title (e.g. `DCD996P2`, `WH-1000XM5`) must appear in the eBay title. At least 60% of the other key words must match. Listings that say parts, broken, replacement, "compatible with", case, charger, lot of, and similar are thrown out. Listings priced under 40% of the Woot price are excluded so accessories don't drag the numbers down.

## Limits

- **eBay comps are active listings, not sold ones.** Sold-price data (Marketplace Insights API) is limited-release, and eBay only grants it to approved partners. The 30th-percentile × 0.90 haircut is there to compensate. Before buying anything, tap **eBay SOLD** on the card and check what it actually sold for.
- **No Facebook Marketplace data.** Meta has no API for it and prohibits scraping. The local estimate is derived from eBay prices, and each card includes an FB search link so you can check by hand. Heavy items (generators, mowers) usually come out ahead locally because the eBay shipping cost kills the margin.
- **Quotas:** Woot allows 1,000 calls/day at 1 per second. eBay Browse allows 5,000/day by default. A full run uses roughly 40–60 Woot calls plus one eBay call per item, and results are cached for 12 hours.
- Woot quantity limits, return windows, and refurb warranty terms are not considered. Read the offer before buying multiples.
