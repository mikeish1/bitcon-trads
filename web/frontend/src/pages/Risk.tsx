import { motion } from "framer-motion";
import { ShieldAlert } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { RiskGauge } from "@/components/trading/RiskGauge";
import { CircuitBreakerBadge, RegimeBadge } from "@/components/trading/StatusBadges";
import { useRisk } from "@/hooks/queries";
import type { GaugeValue } from "@/types/api";

export default function Risk(): JSX.Element {
  const { data: r, isLoading } = useRisk();

  const pctGauges: [GaugeValue, string][] = r
    ? [[r.daily_loss, "%"], [r.weekly_loss, "%"]]
    : [];
  const countGauges: GaugeValue[] = r
    ? [r.consecutive_losses, r.trades_today, r.concurrent_positions, r.total_exposure]
    : [];

  return (
    <div className="space-y-4">
      {/* Circuit breaker banner */}
      <Card className={r?.circuit_breaker_tripped ? "border-neg/60 bg-neg/5" : undefined}>
        <CardContent className="flex flex-wrap items-center justify-between gap-3 p-4">
          <div className="flex items-center gap-3">
            <div className={`rounded-full p-2 ${r?.circuit_breaker_tripped ? "bg-neg/15 text-neg" : "bg-pos/15 text-pos"}`}>
              <ShieldAlert className="size-5" />
            </div>
            <div>
              <p className="text-sm font-semibold">
                {r?.circuit_breaker_tripped ? "Trading paused by circuit breaker" : "All safety systems nominal"}
              </p>
              <p className="text-xs text-muted-foreground">
                {r?.circuit_breaker_tripped
                  ? "The bot has stopped opening new positions after consecutive losses."
                  : "No loss limits or breakers are currently engaged."}
              </p>
            </div>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {r && <CircuitBreakerBadge tripped={r.circuit_breaker_tripped} />}
            {r && <RegimeBadge enabled={r.regime_enabled} on={r.regime_on} />}
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Loss limits</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {isLoading || !r ? (
              <Skeleton className="h-24" />
            ) : (
              pctGauges.map(([g, unit]) => (
                <motion.div key={g.key} initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                  <RiskGauge gauge={g} unit={unit} />
                </motion.div>
              ))
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader><CardTitle>Exposure &amp; activity limits</CardTitle></CardHeader>
          <CardContent className="space-y-4">
            {isLoading || !r ? (
              <Skeleton className="h-32" />
            ) : (
              countGauges.map((g) => (
                <motion.div key={g.key} initial={{ opacity: 0 }} animate={{ opacity: 1 }}>
                  <RiskGauge gauge={g} unit={g.key === "total_exposure" ? undefined : undefined} />
                </motion.div>
              ))
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
