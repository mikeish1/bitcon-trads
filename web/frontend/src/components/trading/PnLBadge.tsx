import { ArrowDownRight, ArrowUpRight, Minus } from "lucide-react";
import { cn, fmtUsdSigned, fmtPctSigned } from "@/lib/utils";

/**
 * Signed P&L display. Color AND an arrow icon AND a sign encode direction, so the
 * meaning never depends on color alone (accessibility).
 */
interface PnLBadgeProps {
  usd?: number | null;
  pct?: number | null;
  className?: string;
  iconOnly?: boolean;
  size?: "sm" | "md" | "lg";
}

export function PnLBadge({ usd, pct, className, size = "md" }: PnLBadgeProps): JSX.Element {
  const v = usd ?? pct ?? 0;
  const dir = v > 0 ? "pos" : v < 0 ? "neg" : "flat";
  const Icon = dir === "pos" ? ArrowUpRight : dir === "neg" ? ArrowDownRight : Minus;
  const color = dir === "pos" ? "text-pos" : dir === "neg" ? "text-neg" : "text-muted-foreground";
  const sizeCls = size === "lg" ? "text-lg" : size === "sm" ? "text-xs" : "text-sm";

  return (
    <span className={cn("inline-flex items-center gap-1 tnum font-medium", color, sizeCls, className)}>
      <Icon className="size-3.5 shrink-0" aria-hidden />
      {usd !== undefined && usd !== null && <span>{fmtUsdSigned(usd)}</span>}
      {pct !== undefined && pct !== null && (
        <span className={cn(usd !== undefined && usd !== null && "text-xs opacity-80")}>
          {fmtPctSigned(pct)}
        </span>
      )}
    </span>
  );
}
