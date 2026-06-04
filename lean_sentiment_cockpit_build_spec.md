# Lean Sentiment Cockpit — Build Spec (for Claude Code)

A secondary, glance-able dashboard for pre-open review and intraday sentiment context. It does **not** duplicate the primary charting/order-book terminal beyond a compact price backup, and it contains **no model/prediction output** in this phase.

---

## 1. Scope

- Lean cockpit. Every tile must be useful for sentiment insight at a glance during market hours.
- No modeling, no prediction/sentiment-summary section in this build.
- **Every tile has an on-hover pop-up** (tooltip) containing (a) a short interpretation of how to read the number, and (b) its freshness. Freshness is shown only inside the tooltip — there are no freshness chips on the tiles themselves.

---

## 2. Data sources — two databases

| Alias | Database | What it holds |
|---|---|---|
| `sentiment_db` | the `stock_sentiment_db` already documented | All daily/end-of-day and event data: OHLCV, derivatives, FII/DII, breadth, fundamentals, global indices, GIFT Nifty, announcements, board meetings, corporate actions, news, etc. |
| `live_db` | the real-time database (schema not yet provided) | All intraday data: live quotes, live indices, pre-open auction, order book. |

**Important:** `live_db` table/column names below are **placeholders**, because its schema hasn't been provided yet. Keep them in the tile registry (Section 8) so they can be swapped for the real names in one place. Section 4 lists exactly what `live_db` must expose.

Both connections are **read-only**.

---

## 3. Global UI rules

- **Tooltip on every tile** = interpretation sentence + freshness line. Trigger on hover *and* keyboard focus (accessibility).
- A single screen-level `Data as of [date] · [time]` stamp at the top (this is the only time/date shown outside tooltips).
- F&O-only tiles (PCR, max pain) render **"Not in F&O"** for cash-only symbols instead of blanks.
- Round every displayed number sensibly (integers for counts, 1–2 decimals for %, ₹ with thousands separators).

**Layout map:**

```
┌─────────────────────────────────────────────────────────────┐
│  A · Market pulse  (fixed top strip, market-wide)            │
├──────────────────────────────┬──────────────────────────────┤
│  B · Identity                │  C · Price & gap             │
├──────────────────────────────┴───────────────┬──────────────┤
│  D · Volume                                   │              │
├───────────────────────────────────────────────┤  Events      │
│  E · Sentiment core (3-column)                 │  rail        │
├───────────────────────────────────────────────┤  (right)     │
│  F · Risk strip                                │              │
└────────────────────────────────────────────────┴─────────────┘
```

---

## 4. What `live_db` must expose

These are the real-time fields the cockpit needs. Confirm the actual table/column names against the live schema and update the registry.

- **Live stock quote** (per symbol): last traded price, % change, today's open, cumulative volume
- **Live index values**: Nifty 50 (level + %), India VIX (level + change), each NSE sectoral index (level + %)
- **Pre-open auction** (per symbol, 9:00–9:15): IEP and indicative quantity
- **Order book top-of-book** (per symbol): best bid, best ask
- **Daily circuit band %** per symbol (may live here or as a daily reference table — confirm)

Most of these almost certainly already flow into the primary dashboard's pipeline — reuse it rather than building a parallel feed. Index constituent lists (Nifty 50 / Nifty 500) for the identity badge are in neither documented schema — add a small reference table.

---

## 5. Section A — Market pulse

Fixed top strip, market-wide, same for every symbol. Read once every few minutes, not per stock.

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| A1 | India VIX (value + % chg) | `live_db` VIX level + change (confirm) | The market's fear gauge. Rising VIX = more fear and bigger swings, so trade smaller with wider stops; falling = calm. *Freshness: live.* |
| A2 | GIFT Nifty premium/discount vs prev close | `sentiment_db.features_daily.gift_nifty_premium_pct` (+ `gift_nifty.gift_nifty_points`) | Offshore Nifty futures that trade before NSE opens — your best preview of the opening direction. A positive premium hints at a higher open. *Freshness: pre-open snapshot.* |
| A3 | Nifty 50 (value + % chg) | `live_db` Nifty 50 spot (confirm) | The market's heartbeat; most stocks drift with it. Trending up = tailwind for longs; falling sharply = pause longs. *Freshness: live.* |
| A4 | Previous day A/D ratio | `sentiment_db.advance_decline` (adv/decl/ratio); `features_daily.ad_ratio_3d_ma` | Yesterday's breadth — how many stocks rose vs fell. Strong breadth = broad participation; weak breadth warns even if the index looks fine. *Freshness: previous session.* |
| A5 | Sectoral index heatmap | `live_db` sectoral index values/% (confirm) | Shows where money is flowing today by sector. Favour green sectors for longs and red for shorts; fighting a red sector lowers your odds. *Freshness: live.* |
| A6 | Global index data (pre-market) | `sentiment_db.global_indices` (+ `features_daily` americas/europe/apac signals, global_consensus/magnitude/divergence) | Overnight moves in US/Europe/Asia that set the mood before open. Broad green overseas usually means a firmer open here. *Freshness: overnight / pre-open.* |

---

## 6. Section B — Identity

Static per symbol, loads on selection.

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| B1 | Name, index, F&O badge | name → `sentiment_db.universe_definition.company_name`; F&O → `universe_definition.f&o_flag` / `features_daily.is_fo_stock`; index membership → constituent list (add ref) | Identifies the stock, whether it's F&O-eligible (unlocks PCR / max pain), and its index membership. *Freshness: static reference.* |
| B2 | Sector tag | `sentiment_db.universe_definition.sector` (+ `sectoral_index`) | The stock's sector — always read a stock with its sector context, since sector moves dominate. *Freshness: static reference.* |
| B3 | Market cap + category | value → `sentiment_db.daily_ohlcv.market_cap`; category → `universe_definition.market_cap_category` / `features_daily.market_cap_category` | Size and liquidity band. Large caps = tighter spreads and cleaner action; small caps = thinner, more volatile, circuit-prone. *Freshness: previous close.* |
| B4 | PE ratio | `sentiment_db.daily_ohlcv.pe_ratio` (or `fundamentals.pe_ratio`; `features_daily.pe_vs_sector_median`) | Valuation context — how expensive the stock is vs earnings. Background only, not an intraday trigger. *Freshness: TTM / previous close.* |

---

## 7. Section C — Price & gap

Highest-priority real estate. This is the **compact backup** of the primary price view — keep it lean.

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| C1 | Stock LTP + % chg (chart) | LTP/% → `live_db` quote (confirm); chart line → `sentiment_db.minute_ohlcv` | Live price and today's move, with an intraday line — your backup if the primary terminal is down. *Freshness: live.* |
| C2 | Gap % (prev close → today open) | computed (today_open from `live_db`; `sentiment_db.daily_ohlcv.previous_close`) — see appendix | How far the stock jumped overnight. Small gaps in trends tend to continue; large gaps (>2%) without news often fade. *Freshness: set at open.* |
| C3 | Previous day OHLC | `sentiment_db.daily_ohlcv.open/high/low/close` (bold high & low) | Yesterday's range; the high (PDH) and low (PDL) are today's key support/resistance that everyone watches. *Freshness: previous close.* |
| C4 | IEP | `live_db` pre-open auction IEP + qty (confirm); show 9:00–9:15 only | The auction's predicted opening price before 9:15. Heavy IEP volume means big players are committed and the gap is more likely to hold. *Freshness: pre-open only (hides at 9:15).* |
| C5 | VWAP live + distance % | computed from `sentiment_db.minute_ohlcv`; distance uses live LTP — see appendix | The day's volume-weighted fair price that institutions benchmark to. Holding above VWAP = buyers in control; below = sellers. *Freshness: live.* |

---

## 8. Section D — Volume

The "is this move real?" panel.

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| D1 | Live volume + 20D ADV | today → `live_db` cumulative volume; ADV → `AVG(sentiment_db.daily_ohlcv.volume, 20 sessions)`; `features_daily.volume_ratio_1m` / `avg_daily_volume_log` | Is the move backed by participation? Volume well above the 20-day average confirms breakouts; low volume = likely trap. *Freshness: live (ADV from history).* |
| D2 | Volume pace | computed (today_volume ÷ session fraction, vs ADV) — see appendix | Time-adjusted volume so 10 AM and 2 PM compare fairly; pace over 100% means unusually active today. *Freshness: live.* |
| D3 | Delivery % (prev trading day) | `sentiment_db.daily_ohlcv.delivery_pct` (+ `delivery_quantity`); `features_daily.delivery_pct_ratio` | Share of yesterday's volume actually held overnight (India-specific). High = genuine accumulation with legs; low = speculative churn. *Freshness: previous session.* |

---

## 9. Section E — Sentiment core

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| E1 | PCR | `sentiment_db.derivatives_daily.pcr` (+ `features_daily.pcr` / `pcr_change`); F&O only | Put vs call activity. Above ~1.2 leans bearish, below ~0.8 bullish — but extremes can flip contrarian, so weigh it, don't obey it. *Freshness: previous close (not live unless an options feed is added).* |
| E2 | Max pain | `sentiment_db.derivatives_daily.max_pain_strike` (+ `features_daily.max_pain_distance_pct`); distance vs live LTP; F&O only | The strike where most options expire worthless; price tends to gravitate there, most strongly near expiry. *Freshness: previous close (level).* |
| E3 | 52-week high/low + LTP % distance | levels → MAX/MIN over ~252 sessions of `sentiment_db.daily_ohlcv`; `features_daily.proximity_52wk`; distance uses live LTP — see appendix | Position within the yearly range. Near the high = momentum / breakout watch; near the low = weakness or reversal watch. *Freshness: live distance (levels from history).* |
| E4 | Stock vs sector performance | computed (stock %_today − sector %_today); sector % from `live_db` mapped via `universe_definition.sectoral_index`; `features_daily.rs_vs_sector_5d` | Is the stock leading or lagging its sector today? Outperforming a green sector = real conviction; lagging = relative weakness. Works even if the stock isn't a formal constituent. *Freshness: live.* |

---

## 10. Section F — Risk strip

A single row of guardrails.

| # | Tile | Source / formula | Tooltip (interpretation + freshness) |
|---|---|---|---|
| F1 | Circuit breaker (band + upper/lower ₹) | band % → `live_db` or daily ref (confirm); upper/lower computed from `sentiment_db.daily_ohlcv.previous_close` — see appendix | The daily price limit where trading halts. Near a circuit, liquidity dries up and you may be unable to exit — know the levels in advance. *Freshness: set at open (band updated daily).* |
| F2 | ATR (14-day, ₹ and %) | computed 14-period ATR from `sentiment_db.daily_ohlcv`; `features_daily.atr_normalized` proxy — see appendix | The stock's typical daily range. Size stops at ≥0.5× ATR and targets at 1–1.5× ATR; skip stocks with ATR% under ~1.5%. *Freshness: previous close.* |
| F3 | Bid/ask spread | `live_db` order book — best_bid / best_ask (confirm) | The instant cost to enter and exit. Tight = liquid and safe; wide = illiquid and hard to exit cleanly. *Freshness: live.* |

---

## 11. Events rail (right-hand side)

Latest events for the selected symbol, newest first, each with a type icon + date + one-line summary; filterable by type.

| Event type | Source | Tooltip (interpretation + freshness) |
|---|---|---|
| Announcements | `sentiment_db.announcements` (category / details / broadcast_date) | Company filings that can move the stock. *Freshness: event-driven.* |
| Board meetings | `sentiment_db.board_meetings` (meeting_date / purpose); also drives "results in N days" | Scheduled board meetings (often results); expect bigger moves around these. *Freshness: event-driven.* |
| Corporate actions | `sentiment_db.corporate_actions` (action_type / ex_date / record_date) | Dividends, splits, bonuses; watch the ex-date. *Freshness: event-driven.* |
| Bulk / block deals | `sentiment_db.block_bulk_deals` (buy/sell qty) | Large trades by big players — directional interest. *Freshness: previous session.* |
| News | `sentiment_db.news_sentiment` (headline / source / published_at / article_url) | Latest headlines for this stock. *Freshness: rolling.* |

- `news_sentiment.finbert_score` is already stored and may optionally show as a small sentiment dot on each headline. Note it is a **pre-computed score, not new modeling** in this build, so it stays within the no-model scope. Treat as optional.

---

## 12. Appendix — formulas for computed tiles

```
Gap %            = (today_open − previous_close) / previous_close × 100

VWAP             = Σ(typical_price_i × volume_i) / Σ(volume_i)   cumulative from 09:15
                   typical_price = (high + low + close) / 3   per minute bar
VWAP distance %  = (LTP − VWAP) / VWAP × 100

20D ADV          = AVG(daily_ohlcv.volume) over the last 20 trading sessions
Volume %         = today_cumulative_volume / 20D_ADV × 100
Session fraction = minutes_elapsed_since_0915 / 375        (full NSE session = 375 min)
Volume pace      = today_cumulative_volume / session_fraction       (projected full day)

52W high / low   = MAX(daily_ohlcv.high) / MIN(daily_ohlcv.low) over last ~252 sessions
Dist to 52W high = (LTP − high_52w) / high_52w × 100
Dist to 52W low  = (LTP − low_52w)  / low_52w  × 100

ATR(14)          = 14-period average of True Range, where
                   TR = max( high − low, |high − prev_close|, |low − prev_close| )
ATR %            = ATR / LTP × 100
Typical range    = LTP ± ATR

Circuit upper    = previous_close × (1 + band%)
Circuit lower    = previous_close × (1 − band%)

Bid-ask spread   = best_ask − best_bid
Spread %         = (best_ask − best_bid) / ((best_ask + best_bid) / 2) × 100
```

---

## 13. Build plan for Claude Code

**Two connections.** Configure two read-only DSNs via env vars (e.g. `LIVE_DB_URL`, `SENTIMENT_DB_URL`). Never hardcode credentials.

**Data-driven tile registry.** Define every tile in one config (e.g. `tiles.ts` / `tiles.py`). Each entry:

```
{
  id, label, section,            // identity + placement
  source,                        // which DB + table/column, OR "computed"
  query | compute,               // a query fn (live_db / sentiment_db) or a compute fn over fetched series
  tooltip,                       // interpretation string
  freshness,                     // "live" | "pre-open" | "prev-session" | "prev-close" | "TTM" | "event" | "static"
  foOnly?: boolean,              // PCR, max pain
  refresh,                       // "live" (poll) | "session" (once) | "static"
  display                        // formatter / chart type
}
```

This makes "every tile carries a tooltip with interpretation + freshness" structural, not per-component, and keeps all `live_db` placeholders in one swappable place.

**Tooltip component.** One reusable popover that reads `tooltip` + `freshness` from the registry and shows them on hover and on focus. For tiles with `foOnly` and a cash-only symbol, render "Not in F&O" instead of the value.

**Refresh / caching.**
- `refresh: "live"` → poll `live_db` on a short interval (≈2–5s); compute tiles recompute on each tick.
- `refresh: "session"` → fetch once on symbol select / at open (daily and pre-open data).
- `refresh: "static"` → fetch once (identity).
- Drive the screen-level `Data as of` stamp from the latest live tick.

**Suggested build order (milestones):**
1. Scaffold, two DB connections, tile registry, tooltip component, layout grid.
2. Section A (market pulse) + Section C (price & gap) — exercises both DBs and the live poll.
3. Section D (volume) + Section E (sentiment core) — ADV, VWAP, pace, PCR, max pain, 52W, relative strength.
4. Section F (risk strip) — circuit, ATR, spread.
5. Events rail.
6. Polish: tooltip copy, F&O handling, freshness, number formatting, empty/loading states.

**Before coding live tiles:** confirm the `live_db` schema and replace the placeholder table/column names in the registry. Until then, those tiles can be stubbed with mock data so the rest of the cockpit is fully buildable.
