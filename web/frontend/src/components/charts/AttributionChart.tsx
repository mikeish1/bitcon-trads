import { Bar, BarChart, Cell, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { fmtUsdSigned } from "@/lib/utils";
import type { CoinAttribution } from "@/types/api";

/** Per-coin realized P&L bars (green for net-positive coins, red for net-negative). */
export function AttributionChart({ data }: { data: CoinAttribution[] }): JSX.Element {
  return (
    <ResponsiveContainer width="100%" height={Math.max(160, data.length * 38)}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
        <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" horizontal={false} />
        <XAxis
          type="number"
          tickFormatter={(v: number) => fmtUsdSigned(v)}
          tick={{ fill: "hsl(var(--muted-foreground))", fontSize: 11 }}
          stroke="hsl(var(--border))"
        />
        <YAxis
          type="category"
          dataKey="base"
          tick={{ fill: "hsl(var(--foreground))", fontSize: 12 }}
          stroke="hsl(var(--border))"
          width={48}
        />
        <Tooltip
          cursor={{ fill: "hsl(var(--muted))", opacity: 0.3 }}
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            fontSize: 12,
          }}
          formatter={(value: number, _n, item) => [
            fmtUsdSigned(value),
            `${(item?.payload as CoinAttribution).closed_trades} trades · ${(item?.payload as CoinAttribution).win_rate_pct}% win`,
          ]}
        />
        <Bar dataKey="realized_pnl_usd" radius={[0, 4, 4, 0]}>
          {data.map((d) => (
            <Cell key={d.base} fill={d.realized_pnl_usd >= 0 ? "hsl(var(--pos))" : "hsl(var(--neg))"} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

export default AttributionChart;
