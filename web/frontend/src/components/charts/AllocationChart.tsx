import { Cell, Pie, PieChart, ResponsiveContainer, Tooltip } from "recharts";
import { baseOf, fmtUsd } from "@/lib/utils";
import type { OpenPosition } from "@/types/api";

const PALETTE = [
  "hsl(var(--primary))",
  "hsl(var(--pos))",
  "hsl(var(--regime))",
  "hsl(var(--warn))",
  "hsl(199 70% 70%)",
  "hsl(280 60% 65%)",
];

/** Current allocation by position market value, plus free cash, as a donut. */
export function AllocationChart({ positions, cash }: { positions: OpenPosition[]; cash: number }): JSX.Element {
  const data = [
    ...positions.map((p) => ({ name: baseOf(p.symbol), value: p.market_value })),
    { name: "Cash", value: Math.max(0, cash) },
  ].filter((d) => d.value > 0);

  return (
    <ResponsiveContainer width="100%" height={240}>
      <PieChart>
        <Pie data={data} dataKey="value" nameKey="name" innerRadius={56} outerRadius={88} paddingAngle={2} stroke="none">
          {data.map((d, i) => (
            <Cell key={d.name} fill={d.name === "Cash" ? "hsl(var(--muted))" : PALETTE[i % PALETTE.length]} />
          ))}
        </Pie>
        <Tooltip
          contentStyle={{
            background: "hsl(var(--popover))",
            border: "1px solid hsl(var(--border))",
            borderRadius: 8,
            fontSize: 12,
          }}
          formatter={(value: number, name) => [fmtUsd(value), name as string]}
        />
      </PieChart>
    </ResponsiveContainer>
  );
}

export default AllocationChart;
