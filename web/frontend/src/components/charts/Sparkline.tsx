import { Area, AreaChart, ResponsiveContainer, YAxis } from "recharts";
import type { EquityPoint } from "@/types/api";

/** Tiny inline equity sparkline for the Overview header card. */
export function Sparkline({ points }: { points: EquityPoint[] }): JSX.Element {
  const up = points.length >= 2 && points[points.length - 1]!.equity >= points[0]!.equity;
  const color = up ? "hsl(var(--pos))" : "hsl(var(--neg))";
  return (
    <ResponsiveContainer width="100%" height={48}>
      <AreaChart data={points} margin={{ top: 2, right: 0, bottom: 2, left: 0 }}>
        <defs>
          <linearGradient id="sparkFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity={0.3} />
            <stop offset="100%" stopColor={color} stopOpacity={0} />
          </linearGradient>
        </defs>
        <YAxis hide domain={["dataMin", "dataMax"]} />
        <Area type="monotone" dataKey="equity" stroke={color} strokeWidth={1.5} fill="url(#sparkFill)" isAnimationActive={false} />
      </AreaChart>
    </ResponsiveContainer>
  );
}

export default Sparkline;
