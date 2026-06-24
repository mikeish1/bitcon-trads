# Dashboard sleeve coverage (carry + ETF)

| | |
|---|---|
| **Status** | Implemented |
| **Date** | 2026-06-23 |
| **Extends** | [DASHBOARD_ARCHITECTURE.md](DASHBOARD_ARCHITECTURE.md) (ADR-001) |
| **Scope** | Additive, read-only. No change to any trading loop, risk manager, or the spot-only views. |

## Problem

`src/run_all.py` can run **three** independent bots into one account
(`RUN_BOTS=spot,carry,etf`), all writing to the **same** `trading_state.db`:

| Sleeve | Module | Tables it owns |
|---|---|---|
| spot | `src/main_loop.py` | `state`, `trades`, `decisions` |
| carry | `src/carry/main.py` | `carry_positions`, `carry_funding`, `carry_state` |
| etf | `src/etf/main.py` | `etf_positions`, `etf_state` |

The dashboard (ADR-001) was built around the spot bot only. Every domain
(`/api/summary`, `/positions`, `/trades`, `/risk`, `/performance`, …) reads the
spot tables exclusively. The carry and ETF sleeves — including the ETF re-platform
that is the current go-live focus — were **invisible** except for their
capital-limit settings on the Config page. The dashboard already opens the file
that holds their data; it simply never queried it.

## Change

A new read-only **Sleeves** domain that reads the carry/ETF tables the same way
`web/queries.py` reads the spot tables.

**Backend**
- `web/sleeves.py` — read-only query/compute layer for both sleeves. Raw SQL on
  the existing read-only connection; **never imports `EtfRiskManager` /
  `CarryRiskManager`** (their `__init__` opens a read-write connection and runs
  `CREATE TABLE`/`ALTER TABLE`, which would break the read-only guarantee).
- `web/models.py` — `SleeveCard`, `SleevesOverview`, `EtfSleeve`/`EtfHolding`,
  `CarrySleeve`/`CarryPair`/`CarryFundingPoint`.
- `web/routers/sleeves.py` — `GET /api/sleeves`, `/api/sleeves/etf`,
  `/api/sleeves/carry`; registered in `web/server.py`.

**Frontend** — `web/frontend/src/pages/Sleeves.tsx` (+ nav entry, route, types,
api client, query hooks, glossary terms). A single page: a three-sleeve overview
strip, an ETF holdings section, and a carry delta-neutral-pairs section with a
daily-funding sparkline.

## Design decisions (why it's safe and correct)

1. **No live prices required.** The dashboard's price feed is crypto-only
   (Binance.US public ticker). ETF holdings are equities it cannot quote, so open
   ETF holdings show at **cost basis** with an explicit "live price unavailable"
   note and `price_is_stale=true`; **realized P&L on closed ETF positions is
   exact**. Carry pairs are delta-neutral, so they are fully described from the DB
   (funding accrued, notional, realized) without prices. This mirrors the existing
   `equity_basis: "approx"` honesty pattern.
2. **Graceful absence.** Each builder checks the sleeve's tables exist; a sleeve
   that never ran reports `available=false` and the UI shows a calm empty state,
   never an error (a DB with only spot tables is a first-class case).
3. **Read-only preserved.** `test_web_sleeves.py` asserts writes to the carry/ETF
   tables are rejected, and the existing `test_web_readonly.py` import-graph guard
   still passes (no order surface imported).

## Tests

`tests/test_web_sleeves.py` + `seed_sleeve_tables()` in `tests/conftest_web.py`:
query-layer shapes, ETF cost-basis fallback, carry pairs/funding aggregation,
graceful-absence, the three HTTP endpoints, and the read-only guarantee on the new
tables.

## Known follow-ups (not in this change)

- Closed carry/ETF trade **history** (this change shows open positions + realized
  totals; per-pair/per-rotation history tables are a natural next step).
- An optional **equities price source** would unlock live ETF mark-to-market;
  until then ETF open-position MTM is intentionally not asserted.
- The cross-sleeve overview strip could also be surfaced on the main Overview page.
