/**
 * The educational layer: every strategy/risk term in one place, each with a
 * plain-English explanation and the precise formula. Tooltips and the Strategy
 * page read from here so the wording is consistent and maintained in a single spot.
 *
 * Formulas use the bot's configured defaults (config/trading_config.yaml). Where a
 * value is user-tunable, the live value is shown by the component when available.
 */
export interface GlossaryEntry {
  term: string;
  plain: string;
  math?: string;
}

export const glossary = {
  donchian: {
    term: "Donchian breakout",
    plain:
      "The bot buys a coin when its daily close pushes above the highest high of the prior 40 days — a classic trend-entry that only acts on genuine breakouts.",
    math: "Enter long when close > max(high over the previous 40 days)",
  },
  chandelier: {
    term: "Chandelier trailing stop",
    plain:
      "The exit trails below the highest price reached since entry, so winners are given room to run while gains are protected. It only ever ratchets up, never down.",
    math: "stop = peak_price − 3 × ATR(14)",
  },
  atr: {
    term: "ATR (Average True Range)",
    plain:
      "A volatility measure: the typical daily price range over the last 14 days. The stop distance scales with it, so volatile coins get wider stops.",
    math: "ATR(14) = 14-day average of max(high−low, |high−prevClose|, |low−prevClose|)",
  },
  regime: {
    term: "BTC regime filter",
    plain:
      "New entries are only allowed while Bitcoin itself is in an uptrend (its close is above a 100-day moving average); when BTC drops below it, the bot exits everything. Alts are ~0.8 correlated to BTC, so this cuts failed-breakout whipsaw.",
    math: "risk-on when BTC close > SMA(100); else risk-off (flatten all)",
  },
  rMultiple: {
    term: "R-multiple",
    plain:
      "Profit measured in units of the initial risk. +1R means the trade has gained exactly what it originally risked to its first stop.",
    math: "R = (price − entry) ÷ (entry − initial_stop)",
  },
  unrealizedPnl: {
    term: "Unrealized P&L",
    plain: "The paper gain or loss on an open position if it were marked at the current price.",
    math: "unrealized = qty × last_price − entry_cost",
  },
  distanceToStop: {
    term: "Distance to stop",
    plain:
      "How far the current price sits above the active trailing stop, in percent. Smaller means closer to being stopped out.",
    math: "(last_price − current_stop) ÷ last_price",
  },
  perAssetCap: {
    term: "Per-asset cap",
    plain: "No single coin may exceed this share of total equity, to keep the book diversified.",
    math: "position_value ≤ equity × 30%",
  },
  deployableCapital: {
    term: "Deployable capital",
    plain:
      "The single master ceiling on how much capital may be committed to open positions at once. It caps the total envelope; it does not change how capital is ranked across opportunities.",
    math: "open_value ≤ min(max_pct × basis, max_usd)",
  },
  dailyLoss: {
    term: "Daily loss limit",
    plain: "Trading pauses for the rest of the UTC day after equity falls this far from the day's start.",
    math: "(equity − day_start) ÷ day_start ≤ −3%",
  },
  weeklyLoss: {
    term: "Weekly loss limit",
    plain: "Trading pauses for the week after equity falls this far from the week's start.",
    math: "(equity − week_start) ÷ week_start ≤ −7%",
  },
  circuitBreaker: {
    term: "Circuit breaker",
    plain:
      "A safety halt: after this many losing trades in a row, the bot stops opening new positions until conditions reset.",
    math: "halt when consecutive_losses ≥ 4",
  },
  winRate: {
    term: "Win rate",
    plain:
      "Share of closed trades that were profitable. Trend-following systems are often right less than half the time but win big on the trades that run.",
    math: "wins ÷ (wins + losses); shown as 50% until ≥ 10 trades close",
  },
  profitFactor: {
    term: "Profit factor",
    plain: "Gross profit divided by gross loss. Above 1.0 means the strategy made money overall.",
    math: "Σ(winning P&L) ÷ |Σ(losing P&L)|",
  },
  expectancy: {
    term: "Expectancy",
    plain: "The average dollar result per closed trade — what you'd expect to make on a typical trade.",
    math: "mean(P&L per closed trade)",
  },
  maxDrawdown: {
    term: "Max drawdown",
    plain:
      "The largest peak-to-trough fall in equity. The strategy's headline goal is a smaller drawdown than buy-and-hold.",
    math: "min over time of (equity − running_peak) ÷ running_peak",
  },
  firstCome: {
    term: "First-come allocation",
    plain:
      "The default mode: every coin that breaks out is sized independently, in order, under the portfolio caps.",
  },
  momentumRotation: {
    term: "Momentum rotation",
    plain:
      "An optional mode that holds only the K strongest coins (by N-day momentum) that have an active trend, rotating periodically.",
    math: "momentum = close ÷ close(N days ago) − 1; hold top-K",
  },

  // --- Sleeves: the carry & ETF strategies run alongside spot ----------------
  sleeves: {
    term: "Strategy sleeves",
    plain:
      "The supervisor can run three independent strategies into one account: spot trend-following, funding carry, and ETF momentum. Each has its own capital limit, positions and ledger; this page shows all three side by side.",
  },
  fundingCarry: {
    term: "Funding carry",
    plain:
      "A market-neutral strategy: buy the asset on the spot market and short an equal amount of its perpetual future. It earns the funding rate the shorts collect while staying flat to price moves.",
    math: "P&L ≈ funding income − fees (price moves on the two legs cancel)",
  },
  deltaNeutral: {
    term: "Delta-neutral pair",
    plain:
      "Long spot + short perp in equal size, so the position barely moves when the price does. The two legs offset, leaving funding as the return driver. 'Delta drift' flags if the legs fall out of balance (e.g. a partial fill).",
    math: "net delta = spot_qty − perp_qty ≈ 0",
  },
  fundingAccrued: {
    term: "Funding accrued",
    plain:
      "Funding income booked on an open pair so far. It is unrealized until the pair is unwound, at which point it is rolled into realized P&L.",
  },
  carryCapital: {
    term: "Capital used (carry)",
    plain:
      "A cross-venue carry needs cash to buy spot AND margin to short the perp, so capital per pair is larger than the notional. This is the total committed across both venues, capped by the sleeve limit.",
    math: "capital = notional × (1 + 1 ÷ leverage)",
  },
  etfMomentum: {
    term: "ETF momentum sleeve",
    plain:
      "A low-frequency equities sleeve. It holds the strongest trending ETFs (or a fixed-weight basket in static mode) and rebalances on a schedule, rotating out names that lose their trend.",
  },
  etfPriceUnavailable: {
    term: "Live equity price unavailable",
    plain:
      "The read-only dashboard prices crypto from a public feed, but holds no equities data source, so ETF holdings are shown at cost basis. Realized P&L on closed ETF positions is exact; open-position mark-to-market is not computed here.",
  },
} as const satisfies Record<string, GlossaryEntry>;

export type GlossaryKey = keyof typeof glossary;
