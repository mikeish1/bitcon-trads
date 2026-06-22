import { useMemo } from "react";
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { format, parseISO } from "date-fns";
import { fmtUsd, fmtPct } from "@/lib/utils";
import type { EquitySeries } from "@/types/api";

/**
 * Equity curve with a drawdown underlay. Two stacked areas share an x-axis: equity
 * (emerald line/area) on the left axis, drawdown % (rose, ≤ 0) on the right. Uses
 * CSS-variable colors so it tracks the theme.
 */
export function EquityChart({ series }: { series: EquitySeries }): JSX.Element {
  const data = useMemo(
    () =>
      series.points.map((p) => ({
        ts: p.ts,
        equity: p.equity,
        drawdown: p.drawdown_pct,
      })),
    [series.points],
  );

  return (
    <ResponsiveContainer width="100%" height={300}>
      <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
        <defs>
          <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--pos))" stopOpacity={0.35} />
            <stop offset="100%" stopColor="hsl(var(--pos))" stopOpacity={0} />
          </linearGradient>
          <linearGradient id="ddFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="hsl(var(--neg))" stopOpacity={0} />
            <stop offset="100%" stopColor="hsl(var(--neg))" stopOpacity={0.3} />
          </linearGradient>
        </defs>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
        <XAxis
          dataKey="ts"
          tickFormatter={(v: string) => format(parseISO(v), "MMM d")}
          tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
          stroke="hsl(var(--border))"
          minTickGap={40}
        />
        <YAxis
          yAxisId="equity"
          tickFormatter={(v: number) => fmtUsd(v)}
          tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
          stroke="hsl(var(--border))"
          width={64}
          domain={["auto", "auto"]}
        />
        <YAxis yAxisId="dd" orientation="right" hide domain={["dataMin", 0]} />
        <Tooltip
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            fontSize: 12,
          }}
          labelFormatter={(v) => format(parseISO(String(v)), "PPpp")}
          formatter={(value: number, name) =>
            name === "equity" ? [fmtUsd(value), "Equity"] : [fmtPct(value), "Drawdown"]
          }
        />
        <Area
          yAxisId="dd"
          type="monotone"
          dataKey="drawdown"
          stroke="hsl(var(--neg))"
          strokeWidth={1}
          fill="url(#ddFill)"
          isAnimationActive={false}
        />
        <Area
          yAxisId="equity"
          type="monotone"
          dataKey="equity"
          stroke="hsl(var(--pos))"
          strokeWidth={2}
          fill="url(#equityFill)"
          isAnimationActive={false}
        />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export default EquityChart;
