import { AlertTriangle } from "lucide-react";
import { Card } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { PnLBadge } from "@/components/trading/PnLBadge";
import { InfoTip } from "@/components/trading/InfoTip";
import { baseOf, fmtUsd, fmtNum, fmtPct, cn } from "@/lib/utils";
import type { OpenPosition } from "@/types/api";

/** Mobile-first position card (the table collapses to these on small screens). */
export function PositionCard({ p, onClick }: { p: OpenPosition; onClick?: () => void }): JSX.Element {
  // Closer to the stop => fuller, warmer bar. Clamp the "headroom" to 25% for scale.
  const headroom = Math.min(100, (p.distance_to_stop_pct / 25) * 100);
  const tone = p.distance_to_stop_pct < 5 ? "neg" : p.distance_to_stop_pct < 12 ? "warn" : "pos";

  return (
    <Card
      onClick={onClick}
      role={onClick ? "button" : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={(e) => onClick && (e.key === "Enter" || e.key === " ") && onClick()}
      className={cn(
        "p-4 transition-colors",
        onClick && "cursor-pointer hover:border-primary/50 focus-visible:border-primary",
      )}
    >
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <span className="font-semibold">{baseOf(p.symbol)}</span>
          <Badge variant="outline" className="text-[10px]">
            {fmtNum(p.qty, 6)}
          </Badge>
          {p.price_is_stale && (
            <Badge variant="warn" className="gap-1 text-[10px]">
              <AlertTriangle className="size-3" /> stale
            </Badge>
          )}
        </div>
        <PnLBadge usd={p.unrealized_pnl_usd} pct={p.unrealized_pnl_pct} />
      </div>

      <dl className="mt-3 grid grid-cols-3 gap-2 text-xs">
        <Field label="Entry">{fmtUsd(p.entry_price, true)}</Field>
        <Field label="Last">{fmtUsd(p.last_price, true)}</Field>
        <Field label="Value">{fmtUsd(p.market_value)}</Field>
        <Field label="Stop">{fmtUsd(p.current_stop, true)}</Field>
        <Field label={<>R <InfoTip term="rMultiple" /></>}>{fmtNum(p.r_multiple, 2)}R</Field>
        <Field label="% of cap">{fmtPct(p.pct_of_per_asset_cap, 0)}</Field>
      </dl>

      <div className="mt-3 space-y-1">
        <div className="flex items-center justify-between text-[11px] text-muted-foreground">
          <span className="flex items-center gap-1">
            Distance to stop <InfoTip term="distanceToStop" />
          </span>
          <span className={cn("tnum", tone === "neg" && "text-neg", tone === "warn" && "text-warn")}>
            {fmtPct(p.distance_to_stop_pct)}
          </span>
        </div>
        <Progress value={headroom} tone={tone} label={`Distance to stop ${fmtPct(p.distance_to_stop_pct)}`} />
      </div>
    </Card>
  );
}

function Field({ label, children }: { label: React.ReactNode; children: React.ReactNode }): JSX.Element {
  return (
    <div>
      <dt className="flex items-center gap-1 text-[10px] uppercase tracking-wide text-muted-foreground">
        {label}
      </dt>
      <dd className="tnum font-medium">{children}</dd>
    </div>
  );
}
