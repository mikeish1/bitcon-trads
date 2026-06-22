import { Progress } from "@/components/ui/progress";
import { InfoTip } from "@/components/trading/InfoTip";
import { cn, fmtNum } from "@/lib/utils";
import type { GaugeValue } from "@/types/api";

/**
 * A single risk gate as a labelled progress bar. The fill warms (primary → amber →
 * red) as the value approaches its limit, and turns solid red when breached. The
 * dual tooltip carries the plain-English meaning and the exact threshold formula.
 */
export function RiskGauge({ gauge, unit }: { gauge: GaugeValue; unit?: string }): JSX.Element {
  const pct = Math.min(100, gauge.pct_of_limit * 100);
  const tone = gauge.breached ? "neg" : pct >= 80 ? "warn" : pct >= 50 ? "primary" : "pos";

  const fmt = (n: number) => (unit === "%" ? `${fmtNum(n, 1)}%` : fmtNum(n, n < 10 ? 1 : 0));

  return (
    <div className="space-y-1.5">
      <div className="flex items-center justify-between gap-2 text-xs">
        <span className="flex items-center gap-1.5 text-muted-foreground">
          {gauge.label}
          <InfoTip plain={gauge.tooltip_plain} math={gauge.tooltip_math} />
        </span>
        <span className={cn("tnum font-medium", gauge.breached ? "text-neg" : "text-foreground")}>
          {fmt(gauge.current)} <span className="text-muted-foreground">/ {fmt(gauge.limit)}</span>
        </span>
      </div>
      <Progress value={pct} tone={tone} label={`${gauge.label}: ${fmt(gauge.current)} of ${fmt(gauge.limit)}`} />
    </div>
  );
}
