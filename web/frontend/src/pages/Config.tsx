import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { ModeBadge } from "@/components/trading/StatusBadges";
import { CapitalLimitCard } from "@/components/trading/CapitalLimitCard";
import { useConfig, useCapitalLimits } from "@/hooks/queries";

/** Read-only configuration viewer (secrets already stripped server-side) plus the
 * one editable surface: the deployable-capital limit. */
export default function Config(): JSX.Element {
  const { data: cfg, isLoading } = useConfig();
  const { data: limits } = useCapitalLimits();

  if (isLoading || !cfg) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-40" />
        <Skeleton className="h-64" />
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="flex-row flex-wrap items-center justify-between gap-2">
          <CardTitle>Effective configuration</CardTitle>
          <div className="flex items-center gap-2">
            <ModeBadge mode={cfg.mode.mode} />
            {cfg.redacted_keys.length > 0 && (
              <Badge variant="outline">{cfg.redacted_keys.length} secrets redacted</Badge>
            )}
          </div>
        </CardHeader>
        <CardContent>
          <div className="mb-4">
            <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Universe</p>
            <div className="flex flex-wrap gap-1.5">
              {cfg.universe.map((s) => (
                <Badge key={s} variant="secondary">{s}</Badge>
              ))}
            </div>
          </div>
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            <Section title="Strategy" data={cfg.strategy} />
            <Section title="Portfolio caps" data={cfg.portfolio} />
            <Section title="Risk sizing" data={cfg.risk} />
            <Section title="Exits" data={cfg.exits} />
            <Section title="Safety rails" data={cfg.safety} />
            <Section title="Market" data={cfg.market} />
          </div>
        </CardContent>
      </Card>

      {/* The only editable surface. */}
      <div className="grid gap-4 lg:grid-cols-2">
        {limits &&
          Object.values(limits)
            .filter((l) => l.sleeve === "spot")
            .map((l) => <CapitalLimitCard key={l.sleeve} sleeve={l} />)}
      </div>

      {limits && (
        <Card>
          <CardHeader><CardTitle>Other sleeves (read-only)</CardTitle></CardHeader>
          <CardContent className="grid gap-2 sm:grid-cols-2">
            {Object.values(limits)
              .filter((l) => l.sleeve !== "spot")
              .map((l) => (
                <div key={l.sleeve} className="flex items-center justify-between rounded-md border border-border p-3 text-sm">
                  <span className="capitalize">{l.sleeve}</span>
                  <span className="text-xs text-muted-foreground">{l.description ?? "—"}</span>
                </div>
              ))}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

/** Flatten a config section into readable key/value rows. */
function Section({ title, data }: { title: string; data: Record<string, unknown> }): JSX.Element {
  const rows = flatten(data);
  return (
    <div className="rounded-md border border-border p-3">
      <p className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
      <dl className="space-y-1">
        {rows.map(([k, v]) => (
          <div key={k} className="flex items-baseline justify-between gap-3 border-b border-border/30 py-0.5 text-xs">
            <dt className="text-muted-foreground">{k}</dt>
            <dd className="tnum font-medium text-foreground">{v}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function flatten(obj: Record<string, unknown>, prefix = ""): [string, string][] {
  const out: [string, string][] = [];
  for (const [k, v] of Object.entries(obj)) {
    const key = prefix ? `${prefix}.${k}` : k;
    if (v && typeof v === "object" && !Array.isArray(v)) {
      out.push(...flatten(v as Record<string, unknown>, key));
    } else {
      out.push([key, Array.isArray(v) ? v.join(", ") : String(v)]);
    }
  }
  return out;
}
