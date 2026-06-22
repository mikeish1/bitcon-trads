import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { glossary, type GlossaryEntry } from "@/lib/glossary";

/**
 * The dedicated educational section: how the bot makes money and how it protects
 * capital, in plain English plus exact formulas. Content is sourced from the same
 * glossary the inline tooltips use, so wording stays consistent.
 */
export default function Strategy(): JSX.Element {
  return (
    <div className="mx-auto max-w-3xl space-y-4">
      <Card>
        <CardHeader>
          <CardTitle>How this bot trades</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3 text-sm leading-relaxed text-foreground/90">
          <p>
            This is a <b>long-only, multi-coin trend follower</b>. It does not predict prices. It waits
            for a coin to break decisively higher, rides the trend with a stop that trails the price up,
            and exits when the trend reverses. It aims for a <b>smaller drawdown than buy-and-hold</b> by
            cutting losers quickly and letting winners run.
          </p>
          <div className="flex flex-wrap gap-2">
            <Badge variant="pos">Long-only</Badge>
            <Badge variant="primary">Daily timeframe</Badge>
            <Badge variant="regime">BTC-regime filtered</Badge>
            <Badge variant="outline">Paper by default</Badge>
          </div>
        </CardContent>
      </Card>

      <ConceptSection
        title="Entry & exit"
        entries={[glossary.donchian, glossary.chandelier, glossary.atr, glossary.rMultiple]}
      />
      <ConceptSection
        title="Market regime"
        entries={[glossary.regime]}
      />
      <ConceptSection
        title="Allocation"
        entries={[glossary.firstCome, glossary.momentumRotation, glossary.deployableCapital, glossary.perAssetCap]}
      />
      <ConceptSection
        title="Risk & safety"
        entries={[glossary.dailyLoss, glossary.weeklyLoss, glossary.circuitBreaker]}
      />
      <ConceptSection
        title="Reading performance"
        entries={[glossary.winRate, glossary.profitFactor, glossary.expectancy, glossary.maxDrawdown, glossary.unrealizedPnl]}
      />

      <p className="px-1 pb-4 text-xs text-muted-foreground">
        This dashboard is read-only. It never places or modifies orders; the only change it can make is
        adjusting the deployable-capital ceiling (Config page), which the bot applies on its next cycle.
      </p>
    </div>
  );
}

function ConceptSection({ title, entries }: { title: string; entries: GlossaryEntry[] }): JSX.Element {
  return (
    <Card>
      <CardHeader><CardTitle>{title}</CardTitle></CardHeader>
      <CardContent className="space-y-3">
        {entries.map((e) => (
          <div key={e.term} className="border-b border-border/40 pb-3 last:border-0 last:pb-0">
            <p className="text-sm font-semibold text-foreground">{e.term}</p>
            <p className="mt-0.5 text-sm leading-relaxed text-muted-foreground">{e.plain}</p>
            {e.math && (
              <p className="mt-1.5 inline-block rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground">
                {e.math}
              </p>
            )}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
