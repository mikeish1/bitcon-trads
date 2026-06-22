import { useState } from "react";
import { format, parseISO } from "date-fns";
import { Brain } from "lucide-react";
import { motion } from "framer-motion";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { EmptyState } from "@/components/trading/EmptyState";
import { useDecisions } from "@/hooks/queries";
import { baseOf } from "@/lib/utils";

export default function Decisions(): JSX.Element {
  const [symbol, setSymbol] = useState("");
  const [action, setAction] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const { data, isLoading, isFetching } = useDecisions({
    limit: 50,
    cursor,
    symbol: symbol || undefined,
    action: action || undefined,
  });

  return (
    <Card>
      <CardHeader className="gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <CardTitle>Decision log</CardTitle>
          <div className="flex items-center gap-2">
            <Input
              className="h-8 w-36"
              placeholder="Coin e.g. ETH/USDT"
              value={symbol}
              onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setCursor(null); }}
              aria-label="Filter by symbol"
            />
            <Select
              className="h-8"
              value={action}
              onChange={(e) => { setAction(e.target.value); setCursor(null); }}
              aria-label="Filter by action"
              options={[
                { value: "", label: "All actions" },
                { value: "BUY", label: "BUY" },
                { value: "SELL", label: "SELL" },
                { value: "HOLD", label: "HOLD" },
              ]}
            />
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-2">
        {isLoading ? (
          <div className="space-y-2">{Array.from({ length: 6 }).map((_, i) => <Skeleton key={i} className="h-16" />)}</div>
        ) : data && data.items.length > 0 ? (
          data.items.map((d, i) => (
            <motion.div
              key={d.id}
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ delay: Math.min(i * 0.015, 0.2) }}
              className="rounded-md border border-border bg-muted/20 p-3"
            >
              <div className="mb-1 flex flex-wrap items-center gap-2 text-xs">
                <Badge variant={d.action === "BUY" ? "pos" : d.action === "SELL" ? "neg" : "default"}>
                  {d.action}
                </Badge>
                <span className="font-medium">{d.symbol ? baseOf(d.symbol) : "—"}</span>
                <span className="text-muted-foreground tnum">{format(parseISO(d.ts), "PP p")}</span>
                {d.conviction > 0 && <ConvictionDots n={d.conviction} />}
                {d.consulted_claude && <Badge variant="primary">🧠 Claude consulted</Badge>}
              </div>
              <p className="text-sm leading-relaxed text-foreground/90">{d.reasoning}</p>
            </motion.div>
          ))
        ) : (
          <EmptyState icon={<Brain className="size-7" />} title="No decisions match" description="Adjust the filters, or wait for the next cycle to log a decision." />
        )}

        {data && (data.has_more || cursor !== null) && (
          <div className="flex items-center justify-end gap-2 pt-2">
            <Button variant="outline" size="sm" disabled={cursor === null} onClick={() => setCursor(null)}>Newest</Button>
            <Button variant="outline" size="sm" disabled={!data.has_more || isFetching} onClick={() => data.next_cursor != null && setCursor(data.next_cursor)}>Older →</Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function ConvictionDots({ n }: { n: number }): JSX.Element {
  return (
    <span className="inline-flex items-center gap-0.5" aria-label={`Conviction ${n}`}>
      {Array.from({ length: Math.min(n, 5) }).map((_, i) => (
        <span key={i} className="size-1.5 rounded-full bg-primary" />
      ))}
    </span>
  );
}
