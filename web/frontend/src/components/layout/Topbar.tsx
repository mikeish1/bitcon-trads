import { motion } from "framer-motion";
import { Sun, Moon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { ModeBadge, RegimeBadge } from "@/components/trading/StatusBadges";
import { InfoTip } from "@/components/trading/InfoTip";
import { ConnectionStatus } from "@/components/trading/ConnectionStatus";
import { useSummary, useRisk } from "@/hooks/queries";
import { cn, fmtUsd, fmtPctSigned, pnlColor } from "@/lib/utils";
import { useTheme } from "@/hooks/useTheme";

/** Sticky top bar: live equity + day return, the mode/regime badges (impossible to
 * miss), connection status, and a theme toggle. */
export function Topbar(): JSX.Element {
  const { data: summary, isLoading } = useSummary();
  const { data: risk } = useRisk();
  const { theme, toggle } = useTheme();

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-border bg-background/85 px-3 backdrop-blur sm:px-5">
      <div className="flex min-w-0 items-center gap-3">
        {isLoading || !summary ? (
          <Skeleton className="h-7 w-44" />
        ) : (
          <div className="flex min-w-0 items-baseline gap-2">
            <span className="truncate text-base font-semibold tnum sm:text-lg">
              {fmtUsd(summary.equity)}
            </span>
            {summary.equity_basis === "approx" && (
              <InfoTip plain="Equity is approximate — in broker/live mode the read-only dashboard has no exchange keys and values cash from the paper ledger.">
                <span className="text-[10px] font-medium text-warn">approx</span>
              </InfoTip>
            )}
            <motion.span
              key={summary.day_return_pct}
              initial={{ opacity: 0.5 }}
              animate={{ opacity: 1 }}
              className={cn("text-xs font-medium tnum", pnlColor(summary.day_return_pct))}
            >
              {fmtPctSigned(summary.day_return_pct)} today
            </motion.span>
          </div>
        )}
        <span className="hidden text-xs text-muted-foreground sm:inline">
          {summary
            ? `${summary.mode.exchange_id} · ${summary.mode.quote_ccy} · prices ${Math.round(summary.price_age_seconds)}s`
            : ""}
        </span>
      </div>

      <div className="flex items-center gap-2 sm:gap-3">
        {risk && <RegimeBadge enabled={risk.regime_enabled} on={risk.regime_on} className="hidden sm:inline-flex" />}
        {summary && <ModeBadge mode={summary.mode.mode} />}
        <ConnectionStatus />
        <Button
          variant="ghost"
          size="icon"
          onClick={toggle}
          aria-label={`Switch to ${theme === "dark" ? "light" : "dark"} theme`}
        >
          {theme === "dark" ? <Sun className="size-4" /> : <Moon className="size-4" />}
        </Button>
      </div>
    </header>
  );
}
