import { lazy, Suspense, useState } from "react";
import { Link } from "react-router-dom";
import { Inbox, ArrowRight } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Button } from "@/components/ui/button";
import { MetricCard } from "@/components/trading/MetricCard";
import { InfoTip } from "@/components/trading/InfoTip";
import { RiskGauge } from "@/components/trading/RiskGauge";
import { PositionCard } from "@/components/trading/PositionCard";
import { EmptyState } from "@/components/trading/EmptyState";
import { CircuitBreakerBadge, RegimeBadge } from "@/components/trading/StatusBadges";
import { TradeDetailDialog } from "@/components/trading/TradeDetailDialog";
import { useSummary, usePositions, useRisk, useEquity, useDecisions } from "@/hooks/queries";
import { fmtUsd, fmtPctSigned, fmtUsdSigned, pnlColor, baseOf } from "@/lib/utils";

const Sparkline = lazy(() => import("@/components/charts/Sparkline"));

export default function Overview(): JSX.Element {
  const summary = useSummary();
  const positions = usePositions();
  const risk = useRisk();
  const equity = useEquity("30d");
  const decisions = useDecisions({ limit: 6 });
  const [openTrade, setOpenTrade] = useState<number | null>(null);

  const s = summary.data;
  const r = risk.data;

  return (
    <div className="space-y-4">
      {/* KPI strip */}
      <section className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-6">
        <MetricCard label="Equity" loading={summary.isLoading} value={fmtUsd(s?.equity)} term="deployableCapital"
          sub={s ? (
            <span className="flex items-center gap-1">
              {fmtUsd(s.cash)} cash
              {s.equity_basis === "approx" && (
                <InfoTip
                  plain="Equity is approximate. In broker/live mode the read-only dashboard has no exchange keys, so it values cash from the paper ledger rather than the live account balance."
                >
                  <span className="text-warn">· approx</span>
                </InfoTip>
              )}
            </span>
          ) : undefined} />
        <MetricCard label="Day" loading={summary.isLoading} tone={(s?.day_return_pct ?? 0) >= 0 ? "pos" : "neg"}
          value={fmtPctSigned(s?.day_return_pct)} />
        <MetricCard label="Week" loading={summary.isLoading} tone={(s?.week_return_pct ?? 0) >= 0 ? "pos" : "neg"}
          value={fmtPctSigned(s?.week_return_pct)} />
        <MetricCard label="Unrealized" loading={summary.isLoading} term="unrealizedPnl"
          tone={(s?.unrealized_pnl_usd ?? 0) >= 0 ? "pos" : "neg"} value={fmtUsdSigned(s?.unrealized_pnl_usd)} />
        <MetricCard label="Open" loading={summary.isLoading} value={s?.open_positions ?? "—"}
          sub={s ? `${s.trades_today} today` : undefined} />
        <MetricCard label="Win rate" loading={summary.isLoading} term="winRate"
          value={s ? `${s.win_rate_pct}%` : "—"} sub={s ? `${s.wins}W / ${s.losses}L` : undefined} />
      </section>

      <div className="grid gap-4 lg:grid-cols-3">
        {/* Left: equity + risk */}
        <div className="space-y-4 lg:col-span-2">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Equity curve · 30D</CardTitle>
              {s && (
                <span className={`text-sm font-medium tnum ${pnlColor(s.week_return_pct)}`}>
                  {fmtPctSigned(s.week_return_pct)} wk
                </span>
              )}
            </CardHeader>
            <CardContent>
              {equity.isLoading ? (
                <Skeleton className="h-16 w-full" />
              ) : equity.data?.available && equity.data.points.length > 1 ? (
                <Suspense fallback={<Skeleton className="h-16 w-full" />}>
                  <Sparkline points={equity.data.points} />
                </Suspense>
              ) : (
                <p className="py-6 text-center text-xs text-muted-foreground">
                  Equity history is still being collected — the curve appears once the snapshot
                  sampler has a few data points.
                </p>
              )}
              <div className="mt-2 text-right">
                <Button asChild variant="link" size="sm" className="h-auto p-0 text-xs">
                  <Link to="/performance">Full analytics <ArrowRight className="size-3" /></Link>
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Risk &amp; circuit breakers</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              {risk.isLoading || !r ? (
                <div className="space-y-3">{Array.from({ length: 4 }).map((_, i) => <Skeleton key={i} className="h-8" />)}</div>
              ) : (
                <>
                  <RiskGauge gauge={r.daily_loss} unit="%" />
                  <RiskGauge gauge={r.concurrent_positions} />
                  <RiskGauge gauge={r.total_exposure} />
                  <RiskGauge gauge={r.consecutive_losses} />
                  <div className="flex flex-wrap items-center gap-2 pt-1">
                    <CircuitBreakerBadge tripped={r.circuit_breaker_tripped} />
                    <RegimeBadge enabled={r.regime_enabled} on={r.regime_on} />
                  </div>
                </>
              )}
            </CardContent>
          </Card>
        </div>

        {/* Right: positions + decisions */}
        <div className="space-y-4">
          <Card>
            <CardHeader className="flex-row items-center justify-between">
              <CardTitle>Open positions</CardTitle>
              <Badge variant="outline">{s?.open_positions ?? 0}</Badge>
            </CardHeader>
            <CardContent className="space-y-2">
              {positions.isLoading ? (
                <Skeleton className="h-28" />
              ) : positions.data && positions.data.length > 0 ? (
                positions.data.map((p) => <PositionCard key={p.id} p={p} onClick={() => setOpenTrade(p.id)} />)
              ) : (
                <EmptyState
                  icon={<Inbox className="size-7" />}
                  title="No open positions"
                  description="The bot is flat and watching the universe for the next Donchian breakout."
                />
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Recent decisions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {decisions.isLoading ? (
                <Skeleton className="h-24" />
              ) : decisions.data && decisions.data.items.length > 0 ? (
                decisions.data.items.map((d) => (
                  <div key={d.id} className="flex items-start gap-2 text-xs">
                    <Badge variant={d.action === "BUY" ? "pos" : d.action === "SELL" ? "neg" : "default"}>
                      {d.action}
                    </Badge>
                    <div className="min-w-0">
                      <span className="font-medium">{d.symbol ? baseOf(d.symbol) : "—"}</span>
                      <p className="truncate text-muted-foreground">{d.reasoning}</p>
                    </div>
                  </div>
                ))
              ) : (
                <p className="py-4 text-center text-xs text-muted-foreground">No decisions logged yet.</p>
              )}
            </CardContent>
          </Card>
        </div>
      </div>

      <TradeDetailDialog id={openTrade} onClose={() => setOpenTrade(null)} />
    </div>
  );
}
