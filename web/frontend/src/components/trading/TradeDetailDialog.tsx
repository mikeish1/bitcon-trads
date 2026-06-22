import { format, parseISO } from "date-fns";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription } from "@/components/ui/dialog";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { PnLBadge } from "@/components/trading/PnLBadge";
import { useTradeDetail } from "@/hooks/queries";
import { baseOf, fmtUsd, fmtNum, fmtDuration, fmtPctSigned } from "@/lib/utils";

/** Rich trade detail: lifecycle, the exit reason, and the original decision reasoning
 * that preceded the entry (joined server-side by symbol + open time). */
export function TradeDetailDialog({ id, onClose }: { id: number | null; onClose: () => void }): JSX.Element {
  const { data, isLoading } = useTradeDetail(id);

  return (
    <Dialog open={id !== null} onOpenChange={(o) => !o && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2">
            {data ? baseOf(data.trade.symbol) : "Trade"}
            {data?.trade.pnl_usd != null && <PnLBadge usd={data.trade.pnl_usd} pct={data.trade.return_pct} />}
          </DialogTitle>
          <DialogDescription>
            {data ? `Trade #${data.trade.id} · ${data.trade.mode}` : "Loading…"}
          </DialogDescription>
        </DialogHeader>

        {isLoading || !data ? (
          <div className="space-y-3">
            <Skeleton className="h-24" />
            <Skeleton className="h-32" />
          </div>
        ) : (
          <div className="space-y-4">
            <dl className="grid grid-cols-2 gap-3 text-sm sm:grid-cols-3">
              <Item label="Opened">{format(parseISO(data.trade.opened_at), "PP p")}</Item>
              <Item label="Closed">
                {data.trade.closed_at ? format(parseISO(data.trade.closed_at), "PP p") : "Open"}
              </Item>
              <Item label="Hold">{fmtDuration(data.trade.hold_hours)}</Item>
              <Item label="Entry">{fmtUsd(data.trade.entry_price, true)}</Item>
              <Item label="Exit">{data.trade.exit_price ? fmtUsd(data.trade.exit_price, true) : "—"}</Item>
              <Item label="Qty">{fmtNum(data.trade.qty, 6)}</Item>
              <Item label="Cost basis">{fmtUsd(data.trade.cost_usd)}</Item>
              <Item label="Return">{fmtPctSigned(data.trade.return_pct)}</Item>
              <Item label="R-multiple">{data.trade.r_multiple != null ? `${fmtNum(data.trade.r_multiple, 2)}R` : "—"}</Item>
            </dl>

            <div>
              <p className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Exit reason
              </p>
              <Badge variant="outline">{data.trade.reason || "—"}</Badge>
            </div>

            <div>
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Decision trail
              </p>
              {data.decisions.length === 0 ? (
                <p className="text-xs text-muted-foreground">No recorded decisions for this symbol around entry.</p>
              ) : (
                <ul className="space-y-2">
                  {data.decisions.map((d) => (
                    <li key={d.id} className="rounded-md border border-border bg-muted/30 p-2.5 text-xs">
                      <div className="mb-1 flex items-center gap-2">
                        <Badge variant={d.action === "BUY" ? "pos" : d.action === "SELL" ? "neg" : "default"}>
                          {d.action}
                        </Badge>
                        <span className="text-muted-foreground tnum">
                          {format(parseISO(d.ts), "PP p")}
                        </span>
                        {d.consulted_claude && <Badge variant="primary">🧠 Claude</Badge>}
                      </div>
                      <p className="leading-relaxed text-foreground/90">{d.reasoning}</p>
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

function Item({ label, children }: { label: string; children: React.ReactNode }): JSX.Element {
  return (
    <div>
      <dt className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="tnum font-medium">{children}</dd>
    </div>
  );
}
