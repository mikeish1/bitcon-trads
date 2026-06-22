import { motion } from "framer-motion";
import { ShieldCheck, ShieldAlert, TrendingUp, TrendingDown, HelpCircle } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { glossary } from "@/lib/glossary";
import { cn } from "@/lib/utils";
import type { TradingMode } from "@/types/api";

/**
 * The PAPER / PAPER-BROKER / LIVE badge — impossible to miss. LIVE is red with a
 * pulsing dot; paper modes are calm. This is the single most important safety
 * affordance in the whole UI.
 */
export function ModeBadge({ mode, className }: { mode: TradingMode; className?: string }): JSX.Element {
  const isLive = mode === "LIVE";
  const tone = isLive ? "neg" : mode === "PAPER-BROKER" ? "warn" : "pos";
  const dot = isLive ? "bg-neg" : mode === "PAPER-BROKER" ? "bg-warn" : "bg-pos";
  const label =
    mode === "LIVE"
      ? "LIVE · real money"
      : mode === "PAPER-BROKER"
        ? "PAPER · broker sim"
        : "PAPER · simulation";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span>
          <Badge
            variant={tone}
            className={cn("gap-1.5 px-2.5 py-1 text-[11px] font-semibold uppercase tracking-wide", className)}
          >
            <span className="relative flex size-2">
              {isLive && (
                <motion.span
                  className={cn("absolute inline-flex size-full rounded-full opacity-75", dot)}
                  animate={{ scale: [1, 1.8], opacity: [0.7, 0] }}
                  transition={{ duration: 1.4, repeat: Infinity }}
                />
              )}
              <span className={cn("relative inline-flex size-2 rounded-full", dot)} />
            </span>
            {label}
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {isLive
          ? "Orders use REAL funds. Requires PAPER_TRADING=false AND LIVE_TRADING_ENABLED=true."
          : mode === "PAPER-BROKER"
            ? "Paper orders placed on the broker's paper endpoint (realistic fills, no real money)."
            : "Internal simulation against live prices. No orders are sent anywhere."}
      </TooltipContent>
    </Tooltip>
  );
}

/** Regime status pill: cyan when risk-on, muted/red when risk-off, neutral if unknown. */
export function RegimeBadge({
  enabled,
  on,
  className,
}: {
  enabled: boolean;
  on: boolean | null;
  className?: string;
}): JSX.Element {
  if (!enabled) {
    return (
      <Badge variant="outline" className={className}>
        Regime filter off
      </Badge>
    );
  }
  const unknown = on === null;
  const Icon = unknown ? HelpCircle : on ? TrendingUp : TrendingDown;
  const variant = unknown ? "default" : on ? "regime" : "neg";
  const text = unknown ? "Regime: unknown" : on ? "RISK-ON · BTC uptrend" : "RISK-OFF · BTC below MA";
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span>
          <Badge variant={variant} className={cn("gap-1", className)}>
            <Icon className="size-3.5" aria-hidden />
            {text}
          </Badge>
        </span>
      </TooltipTrigger>
      <TooltipContent>
        <p className="font-semibold text-foreground">{glossary.regime.term}</p>
        <p className="mt-1 text-muted-foreground">{glossary.regime.plain}</p>
        <p className="mt-2 rounded bg-muted px-2 py-1 font-mono text-[11px]">{glossary.regime.math}</p>
        {unknown && (
          <p className="mt-2 text-muted-foreground">
            The dashboard reads prices read-only without candle history, so it does not recompute the
            moving average; the bot enforces this live.
          </p>
        )}
      </TooltipContent>
    </Tooltip>
  );
}

/** Circuit-breaker chip — solid red when tripped. */
export function CircuitBreakerBadge({ tripped }: { tripped: boolean }): JSX.Element {
  return (
    <Badge variant={tripped ? "neg" : "pos"} className="gap-1">
      {tripped ? <ShieldAlert className="size-3.5" /> : <ShieldCheck className="size-3.5" />}
      {tripped ? "Circuit breaker TRIPPED" : "Breakers nominal"}
    </Badge>
  );
}
