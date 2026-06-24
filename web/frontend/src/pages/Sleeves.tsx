/**
 * Sleeves — the multi-strategy "single pane of glass".
 *
 * src/run_all.py can run three independent bots into ONE account: spot trend-
 * following, funding carry, and ETF momentum. The rest of the dashboard is spot-
 * only; this page surfaces the carry and ETF sleeves (read-only, from their own
 * tables in the shared DB) so nothing the supervisor trades is invisible.
 *
 * Design notes:
 *  - Carry pairs are delta-neutral, so they're fully describable without live
 *    prices (funding income is the driver).
 *  - ETF holdings are equities the dashboard's crypto price feed can't quote, so
 *    open holdings show at cost basis with an explicit, calm "live price
 *    unavailable" note — realized P&L on closed positions is still exact.
 */
import { Layers, ArrowLeftRight, Building2, Inbox, AlertTriangle, Power } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { MetricCard } from "@/components/trading/MetricCard";
import { EmptyState } from "@/components/trading/EmptyState";
import { PnLBadge } from "@/components/trading/PnLBadge";
import { InfoTip } from "@/components/trading/InfoTip";
import { useSleeves, useEtfSleeve, useCarrySleeve } from "@/hooks/queries";
import { cn, fmtUsd, fmtNum, fmtDuration } from "@/lib/utils";
import type { SleeveCard as SleeveCardT, CarryFundingPoint } from "@/types/api";

export default function Sleeves(): JSX.Element {
  const overview = useSleeves();

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <Layers className="size-5 text-muted-foreground" />
        <h1 className="text-lg font-semibold">Strategy sleeves</h1>
        <InfoTip term="sleeves" />
      </div>
      <p className="-mt-2 max-w-2xl text-xs text-muted-foreground">
        The supervisor can run three strategies into one account. Each keeps its own positions,
        ledger and capital limit. Everything here is read-only.
      </p>

      {/* Overview strip: one tile per sleeve */}
      <section className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        {overview.isLoading || !overview.data
          ? Array.from({ length: 3 }).map((_, i) => <Skeleton key={i} className="h-32" />)
          : overview.data.cards.map((c) => <SleeveSummary key={c.key} card={c} />)}
      </section>

      <EtfSection />
      <CarrySection />
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* Shared bits                                                        */
/* ------------------------------------------------------------------ */

/** Mode pill for sleeve modes (PAPER / PAPER-BROKER / LIVE / SIM). LIVE is red. */
function SleeveModeBadge({ mode }: { mode: string | null }): JSX.Element | null {
  if (!mode) return null;
  const m = mode.toUpperCase();
  const tone = m === "LIVE" ? "neg" : m === "PAPER-BROKER" ? "warn" : "pos";
  return (
    <Badge variant={tone} className="text-[10px] font-semibold uppercase tracking-wide">
      {m === "LIVE" ? "LIVE · real money" : m === "SIM" ? "SIM" : m}
    </Badge>
  );
}

function SleeveSummary({ card }: { card: SleeveCardT }): JSX.Element {
  return (
    <Card className={cn("p-4", !card.active && "opacity-70")}>
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="text-sm font-semibold">{card.label}</span>
          {card.capital_description && <InfoTip plain={card.capital_description} />}
        </div>
        {card.active ? <SleeveModeBadge mode={card.mode} /> : <Badge variant="outline">idle</Badge>}
      </div>

      {card.active ? (
        <>
          <div className="mt-3 flex items-baseline gap-2">
            <span className="text-2xl font-semibold tnum">{fmtUsd(card.primary_value_usd)}</span>
            <span className="text-[11px] uppercase tracking-wide text-muted-foreground">
              {card.primary_label}
            </span>
          </div>
          <div className="mt-2 flex items-center justify-between text-xs text-muted-foreground">
            <span>{card.open_positions} open</span>
            <span className="flex items-center gap-1">
              realized
              {card.realized_pnl_usd === null ? (
                <span className="tnum">—</span>
              ) : (
                <PnLBadge usd={card.realized_pnl_usd} size="sm" />
              )}
            </span>
          </div>
        </>
      ) : (
        <p className="mt-3 text-xs text-muted-foreground">{card.note ?? "Not running."}</p>
      )}
    </Card>
  );
}

/** A tiny, dependency-free bar sparkline for the daily funding series. */
function FundingBars({ series }: { series: CarryFundingPoint[] }): JSX.Element {
  const pts = series.slice(-21);
  const max = Math.max(1e-9, ...pts.map((p) => Math.abs(p.amount_usd)));
  return (
    <div className="flex h-12 items-end gap-0.5" aria-hidden>
      {pts.map((p) => (
        <div
          key={p.day}
          title={`${p.day}: ${fmtUsd(p.amount_usd, true)}`}
          className={cn("w-full rounded-sm", p.amount_usd >= 0 ? "bg-pos/60" : "bg-neg/60")}
          style={{ height: `${Math.max(6, (Math.abs(p.amount_usd) / max) * 100)}%` }}
        />
      ))}
    </div>
  );
}

/* ------------------------------------------------------------------ */
/* ETF momentum                                                       */
/* ------------------------------------------------------------------ */
function EtfSection(): JSX.Element {
  const { data, isLoading } = useEtfSleeve();

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Building2 className="size-4 text-muted-foreground" />
          <CardTitle>ETF momentum</CardTitle>
          <InfoTip term="etfMomentum" />
        </div>
        {data?.available && <SleeveModeBadge mode={data.mode} />}
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <Skeleton className="h-40" />
        ) : !data?.available ? (
          <EmptyState
            icon={<Inbox className="size-7" />}
            title="ETF sleeve not active"
            description="Enable it with RUN_BOTS=spot,etf. Holdings, rebalances and realized P&L appear here once it runs."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <MetricCard label="Equity (est.)" value={fmtUsd(data.equity_estimate)}
                sub={data.paper_cash !== null ? `${fmtUsd(data.paper_cash)} cash` : undefined} />
              <MetricCard label="Holdings (cost)" value={fmtUsd(data.holdings_cost_usd)}
                sub={`${data.open_positions} positions`} />
              <MetricCard label="Realized P&L" tone={data.realized_pnl_usd >= 0 ? "pos" : "neg"}
                value={fmtUsd(data.realized_pnl_usd)} sub="closed positions" />
              <MetricCard label="Last rebalance" value={data.last_rebalance ?? "—"}
                sub={data.regime ? `regime: ${data.regime}` : undefined} />
            </div>

            {!data.priced && data.holdings.length > 0 && (
              <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <AlertTriangle className="size-3.5 text-warn" />
                Open holdings shown at cost basis.
                <InfoTip term="etfPriceUnavailable">
                  <span className="underline decoration-dotted">Why?</span>
                </InfoTip>
              </p>
            )}

            {data.holdings.length === 0 ? (
              <EmptyState icon={<Inbox className="size-7" />} title="No open ETF holdings"
                description="The sleeve is in cash between rebalances." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">Symbol</th>
                      <th className="py-2 pr-3 text-right font-medium">Qty</th>
                      <th className="py-2 pr-3 text-right font-medium">Entry</th>
                      <th className="py-2 pr-3 text-right font-medium">Cost</th>
                      <th className="py-2 pr-3 text-right font-medium">Value</th>
                      <th className="py-2 pr-3 text-right font-medium">Unrealized</th>
                      <th className="py-2 pr-3 text-right font-medium">Age</th>
                    </tr>
                  </thead>
                  <tbody className="tnum">
                    {data.holdings.map((h) => (
                      <tr key={h.id} className="border-b border-border/50 last:border-0">
                        <td className="py-2 pr-3 font-medium">{h.symbol}</td>
                        <td className="py-2 pr-3 text-right">{fmtNum(h.qty, 4)}</td>
                        <td className="py-2 pr-3 text-right">{fmtUsd(h.entry_price)}</td>
                        <td className="py-2 pr-3 text-right">{fmtUsd(h.cost_usd)}</td>
                        <td className="py-2 pr-3 text-right">
                          {h.market_value !== null ? (
                            fmtUsd(h.market_value)
                          ) : (
                            <span className="text-muted-foreground">{fmtUsd(h.cost_usd)} <span className="text-[10px]">cost</span></span>
                          )}
                        </td>
                        <td className="py-2 pr-3 text-right">
                          {h.unrealized_pnl_usd !== null ? (
                            <PnLBadge usd={h.unrealized_pnl_usd} pct={h.unrealized_pnl_pct} size="sm" className="justify-end" />
                          ) : (
                            <span className="text-muted-foreground">—</span>
                          )}
                        </td>
                        <td className="py-2 pr-3 text-right text-muted-foreground">{fmtDuration(h.age_days * 24)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* Funding carry                                                      */
/* ------------------------------------------------------------------ */
function CarrySection(): JSX.Element {
  const { data, isLoading } = useCarrySleeve();

  return (
    <Card>
      <CardHeader className="flex-row items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <ArrowLeftRight className="size-4 text-muted-foreground" />
          <CardTitle>Funding carry</CardTitle>
          <InfoTip term="fundingCarry" />
        </div>
        <div className="flex items-center gap-2">
          {data?.available && data.kill_active && (
            <Badge variant="neg" className="gap-1">
              <Power className="size-3.5" /> Kill switch ON
            </Badge>
          )}
          {data?.available && <SleeveModeBadge mode={data.mode} />}
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {isLoading ? (
          <Skeleton className="h-40" />
        ) : !data?.available ? (
          <EmptyState
            icon={<Inbox className="size-7" />}
            title="Carry sleeve not active"
            description="Enable it with RUN_BOTS=spot,carry. Delta-neutral pairs and funding income appear here once it runs."
          />
        ) : (
          <>
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
              <MetricCard label="Capital used" value={fmtUsd(data.capital_used)} term="carryCapital"
                sub={`${data.open_pairs_count} open pairs`} />
              <MetricCard label="Funding today" tone={data.funding_today_usd >= 0 ? "pos" : "neg"}
                value={fmtUsd(data.funding_today_usd, true)} sub="income accrued" />
              <MetricCard label="Funding (total)" tone={data.funding_total_usd >= 0 ? "pos" : "neg"}
                value={fmtUsd(data.funding_total_usd, true)} />
              <MetricCard label="Realized P&L" tone={data.realized_total_usd >= 0 ? "pos" : "neg"}
                value={fmtUsd(data.realized_total_usd)} sub="closed pairs" />
            </div>

            {data.funding_series.length > 1 && (
              <div>
                <p className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                  Daily funding income
                </p>
                <FundingBars series={data.funding_series} />
              </div>
            )}

            {data.pairs.length === 0 ? (
              <EmptyState icon={<Inbox className="size-7" />} title="No open carry pairs"
                description="The sleeve is flat — waiting for net funding to clear the entry bar." />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-xs">
                  <thead>
                    <tr className="border-b border-border text-left text-muted-foreground">
                      <th className="py-2 pr-3 font-medium">Asset</th>
                      <th className="py-2 pr-3 text-right font-medium">Notional</th>
                      <th className="py-2 pr-3 text-right font-medium">Capital</th>
                      <th className="py-2 pr-3 text-right font-medium">
                        <span className="inline-flex items-center gap-1 justify-end">Δ drift <InfoTip term="deltaNeutral" /></span>
                      </th>
                      <th className="py-2 pr-3 text-right font-medium">
                        <span className="inline-flex items-center gap-1 justify-end">Funding <InfoTip term="fundingAccrued" /></span>
                      </th>
                      <th className="py-2 pr-3 text-right font-medium">Age</th>
                      <th className="py-2 pr-3 text-right font-medium">Status</th>
                    </tr>
                  </thead>
                  <tbody className="tnum">
                    {data.pairs.map((p) => (
                      <tr key={p.id} className="border-b border-border/50 last:border-0">
                        <td className="py-2 pr-3 font-medium">{p.asset}</td>
                        <td className="py-2 pr-3 text-right">{fmtUsd(p.notional_usd)}</td>
                        <td className="py-2 pr-3 text-right">{fmtUsd(p.capital_usd)}</td>
                        <td className={cn("py-2 pr-3 text-right", p.delta_drift_pct > 2 ? "text-warn" : "text-muted-foreground")}>
                          {p.delta_drift_pct.toFixed(2)}%
                        </td>
                        <td className="py-2 pr-3 text-right">
                          <PnLBadge usd={p.funding_accrued_usd} size="sm" className="justify-end" />
                        </td>
                        <td className="py-2 pr-3 text-right text-muted-foreground">{fmtDuration(p.age_hours)}</td>
                        <td className="py-2 pr-3 text-right">
                          {p.unwind_in_progress ? (
                            <Badge variant="warn" className="text-[10px]">unwinding</Badge>
                          ) : (
                            <Badge variant="regime" className="text-[10px]">holding</Badge>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
