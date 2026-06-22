import { lazy, Suspense, useState } from "react";
import { Inbox } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { PositionCard } from "@/components/trading/PositionCard";
import { EmptyState } from "@/components/trading/EmptyState";
import { TradeDetailDialog } from "@/components/trading/TradeDetailDialog";
import { usePositions, useSummary } from "@/hooks/queries";

const AllocationChart = lazy(() => import("@/components/charts/AllocationChart"));

export default function Positions(): JSX.Element {
  const { data, isLoading } = usePositions();
  const summary = useSummary();
  const [openTrade, setOpenTrade] = useState<number | null>(null);

  return (
    <div className="space-y-4">
      <div className="grid gap-4 lg:grid-cols-3">
        <Card className="lg:col-span-2">
          <CardHeader className="flex-row items-center justify-between">
            <CardTitle>Open positions</CardTitle>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="grid gap-3 sm:grid-cols-2">
                {Array.from({ length: 2 }).map((_, i) => <Skeleton key={i} className="h-44" />)}
              </div>
            ) : data && data.length > 0 ? (
              <div className="grid gap-3 sm:grid-cols-2">
                {data.map((p) => <PositionCard key={p.id} p={p} onClick={() => setOpenTrade(p.id)} />)}
              </div>
            ) : (
              <EmptyState
                icon={<Inbox className="size-8" />}
                title="No open positions"
                description="The bot is flat and waiting for the next breakout. Positions appear here the moment one opens."
              />
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Allocation</CardTitle>
          </CardHeader>
          <CardContent>
            {summary.isLoading || isLoading ? (
              <Skeleton className="h-56" />
            ) : (
              <Suspense fallback={<Skeleton className="h-56" />}>
                <AllocationChart positions={data ?? []} cash={summary.data?.cash ?? 0} />
              </Suspense>
            )}
          </CardContent>
        </Card>
      </div>

      <TradeDetailDialog id={openTrade} onClose={() => setOpenTrade(null)} />
    </div>
  );
}
