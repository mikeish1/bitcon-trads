import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { AlertTriangle, Loader2 } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input, Label } from "@/components/ui/input";
import { Progress } from "@/components/ui/progress";
import { InfoTip } from "@/components/trading/InfoTip";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
} from "@/components/ui/dialog";
import { api, getAuthToken } from "@/lib/api";
import { qk } from "@/lib/queryKeys";
import { useCapitalSchema } from "@/hooks/queries";
import { fmtUsd, fmtPct } from "@/lib/utils";
import type { CapitalSimulation, SleeveLimit } from "@/types/api";

/**
 * The Deployable-Capital limit — the dashboard's ONLY mutating control. The flow is
 * deliberately careful: edit → live client+server SIMULATION (no write) → explicit
 * confirm modal → audited PUT. The bot hot-reloads the saved value on its next cycle.
 */
export function CapitalLimitCard({ sleeve }: { sleeve: SleeveLimit }): JSX.Element {
  const qc = useQueryClient();
  const policy = sleeve.policy;
  // Field metadata (help text + bounds) comes from the backend so the form stays in
  // lockstep with the server's validation rules instead of hard-coding them here.
  const { data: schema } = useCapitalSchema();
  const [maxPct, setMaxPct] = useState<string>(policy?.max_pct != null ? String(policy.max_pct) : "");
  const [maxUsd, setMaxUsd] = useState<string>(policy?.max_usd != null ? String(policy.max_usd) : "");
  const [sim, setSim] = useState<CapitalSimulation | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const body = () => ({
    max_pct: maxPct.trim() === "" ? null : Number(maxPct),
    max_usd: maxUsd.trim() === "" ? null : Number(maxUsd),
  });

  // Debounced live simulation as the user edits (read-only on the server).
  useEffect(() => {
    const t = setTimeout(() => {
      api
        .capitalSimulate(sleeve.sleeve, body())
        .then(setSim)
        .catch(() => setSim(null));
    }, 350);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [maxPct, maxUsd, sleeve.sleeve]);

  const save = useMutation({
    mutationFn: () => api.capitalUpdate(sleeve.sleeve, body()),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: qk.capitalLimits });
      qc.invalidateQueries({ queryKey: qk.risk });
      qc.invalidateQueries({ queryKey: qk.config });
      setConfirmOpen(false);
      if (res.shadowed_by_env) {
        toast.warning("Saved, but an environment variable is overriding it on the server.");
      } else {
        toast.success(`Capital limit updated: ${res.description ?? "saved"}`);
      }
    },
    onError: (err: Error) => toast.error(err.message || "Failed to update capital limit"),
  });

  const tokenMissing = getAuthToken() === null;
  const exposurePct = sim?.current_exposure_pct ?? null;

  return (
    <Card>
      <CardHeader className="flex-row items-start justify-between">
        <div>
          <CardTitle className="flex items-center gap-2">
            Deployable capital · {sleeve.sleeve}
            <InfoTip term="deployableCapital" />
          </CardTitle>
          <p className="mt-1 text-xs text-muted-foreground">
            {sleeve.description ?? "—"}
          </p>
        </div>
        <Badge variant="outline" className="capitalize">source: {sleeve.source}</Badge>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="grid grid-cols-2 gap-3">
          <div className="space-y-1">
            <Label htmlFor={`pct-${sleeve.sleeve}`}>Max % of {policy?.basis ?? "equity"}</Label>
            <Input
              id={`pct-${sleeve.sleeve}`}
              inputMode="decimal"
              placeholder="e.g. 0.90"
              value={maxPct}
              onChange={(e) => setMaxPct(e.target.value)}
              aria-describedby={`pct-help-${sleeve.sleeve}`}
            />
            {schema?.fields.max_pct?.help && (
              <p id={`pct-help-${sleeve.sleeve}`} className="text-[10px] text-muted-foreground">
                {schema.fields.max_pct.help}
              </p>
            )}
          </div>
          <div className="space-y-1">
            <Label htmlFor={`usd-${sleeve.sleeve}`}>Max USD (optional)</Label>
            <Input
              id={`usd-${sleeve.sleeve}`}
              inputMode="decimal"
              placeholder="e.g. 1000"
              value={maxUsd}
              onChange={(e) => setMaxUsd(e.target.value)}
              aria-describedby={`usd-help-${sleeve.sleeve}`}
            />
            {schema?.fields.max_usd?.help && (
              <p id={`usd-help-${sleeve.sleeve}`} className="text-[10px] text-muted-foreground">
                {schema.fields.max_usd.help}
              </p>
            )}
          </div>
        </div>

        {/* Live simulation (no write) */}
        <div className="rounded-md border border-border bg-muted/20 p-3 text-xs">
          {sim === null ? (
            <span className="text-muted-foreground">Simulating…</span>
          ) : !sim.valid ? (
            <div className="space-y-1 text-neg">
              <p className="flex items-center gap-1 font-medium"><AlertTriangle className="size-3.5" /> Invalid policy</p>
              {sim.errors.map((e) => <p key={e.field} className="text-muted-foreground">{e.msg}</p>)}
            </div>
          ) : (
            <div className="space-y-2">
              <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
                <Field label="Equity" value={fmtUsd(sim.equity)} />
                <Field label="Committed" value={fmtUsd(sim.committed)} />
                <Field label="Deployable" value={fmtUsd(sim.deployable_capital)} />
                <Field label="Remaining" value={fmtUsd(sim.remaining_capacity)} />
              </div>
              {exposurePct != null && (
                <div className="space-y-1">
                  <div className="flex justify-between text-[11px] text-muted-foreground">
                    <span>Current exposure under this cap</span>
                    <span className="tnum">{fmtPct(exposurePct)}</span>
                  </div>
                  <Progress value={exposurePct} tone={exposurePct > 90 ? "warn" : "primary"} />
                </div>
              )}
              <p className="text-[11px] text-muted-foreground">
                Preview only — nothing is saved until you confirm.
              </p>
            </div>
          )}
        </div>

        <div className="flex items-center justify-between gap-2">
          <p className="text-[11px] text-muted-foreground">
            Saving writes an audited override the bot hot-reloads next cycle.
          </p>
          <Button
            size="sm"
            disabled={!sim?.valid}
            onClick={() => setConfirmOpen(true)}
          >
            Save limit…
          </Button>
        </div>
      </CardContent>

      {/* Confirmation modal — explicit, because this is the only thing that mutates. */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Confirm capital-limit change</DialogTitle>
            <DialogDescription>
              This updates the live deployable-capital ceiling for the <b>{sleeve.sleeve}</b> sleeve.
              The trading bot applies it on its next cycle.
            </DialogDescription>
          </DialogHeader>
          {sim?.valid && (
            <div className="rounded-md border border-border p-3 text-sm">
              <p className="font-medium">{sim.description}</p>
              <p className="mt-1 text-xs text-muted-foreground">
                New deployable envelope: {fmtUsd(sim.deployable_capital)} · remaining headroom{" "}
                {fmtUsd(sim.remaining_capacity)}
              </p>
            </div>
          )}
          {tokenMissing && (
            <p className="flex items-center gap-1 text-xs text-warn">
              <AlertTriangle className="size-3.5" />
              No API token set. If the server requires one, this will be rejected — add it via the
              header prompt.
            </p>
          )}
          <div className="flex justify-end gap-2">
            <Button variant="outline" size="sm" onClick={() => setConfirmOpen(false)}>Cancel</Button>
            <Button size="sm" onClick={() => save.mutate()} disabled={save.isPending}>
              {save.isPending && <Loader2 className="size-3.5 animate-spin" />}
              Confirm &amp; save
            </Button>
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  );
}

function Field({ label, value }: { label: string; value: string }): JSX.Element {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</p>
      <p className="tnum font-medium text-foreground">{value}</p>
    </div>
  );
}
