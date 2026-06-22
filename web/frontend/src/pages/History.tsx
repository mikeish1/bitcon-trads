import { useMemo, useState } from "react";
import {
  type ColumnDef,
  type VisibilityState,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { format, parseISO } from "date-fns";
import { Download, Search, SlidersHorizontal } from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input, Label } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { PnLBadge } from "@/components/trading/PnLBadge";
import { EmptyState } from "@/components/trading/EmptyState";
import { InfoTip } from "@/components/trading/InfoTip";
import { TradeDetailDialog } from "@/components/trading/TradeDetailDialog";
import { useTrades, useTradeAggregates } from "@/hooks/queries";
import { baseOf, fmtUsd, fmtNum, fmtDuration, fmtPctSigned } from "@/lib/utils";
import type { ClosedTrade } from "@/types/api";

const PAGE = 50;

export default function History(): JSX.Element {
  const [symbol, setSymbol] = useState("");
  const [search, setSearch] = useState("");
  const [dateFrom, setDateFrom] = useState("");
  const [dateTo, setDateTo] = useState("");
  const [cursor, setCursor] = useState<number | null>(null);
  const [openTrade, setOpenTrade] = useState<number | null>(null);
  const [visibility, setVisibility] = useState<VisibilityState>({});

  const filters = {
    symbol: symbol || undefined,
    date_from: dateFrom || undefined,
    date_to: dateTo || undefined,
  };
  const { data, isLoading, isFetching } = useTrades({ ...filters, limit: PAGE, cursor });
  const aggregates = useTradeAggregates(filters);

  // Client-side global search over the current page (server already filtered/paginated).
  const rows = useMemo(() => {
    const items = data?.items ?? [];
    if (!search.trim()) return items;
    const q = search.toLowerCase();
    return items.filter(
      (t) => t.symbol.toLowerCase().includes(q) || (t.reason ?? "").toLowerCase().includes(q),
    );
  }, [data?.items, search]);

  const columns = useMemo<ColumnDef<ClosedTrade>[]>(
    () => [
      {
        accessorKey: "closed_at",
        header: "Closed",
        cell: ({ getValue }) => {
          const v = getValue<string | null>();
          return <span className="tnum text-muted-foreground">{v ? format(parseISO(v), "MMM d, HH:mm") : "—"}</span>;
        },
      },
      {
        accessorKey: "symbol",
        header: "Coin",
        cell: ({ getValue }) => <span className="font-medium">{baseOf(getValue<string>())}</span>,
      },
      { accessorKey: "hold_hours", header: "Hold", cell: ({ getValue }) => <span className="tnum">{fmtDuration(getValue<number | null>())}</span> },
      { accessorKey: "entry_price", header: "Entry", cell: ({ getValue }) => <span className="tnum">{fmtUsd(getValue<number>(), true)}</span> },
      { accessorKey: "exit_price", header: "Exit", cell: ({ getValue }) => <span className="tnum">{fmtUsd(getValue<number | null>(), true)}</span> },
      { accessorKey: "return_pct", header: "Return", cell: ({ getValue }) => <span className="tnum">{fmtPctSigned(getValue<number | null>())}</span> },
      {
        accessorKey: "pnl_usd",
        header: "P&L",
        cell: ({ getValue }) => <PnLBadge usd={getValue<number | null>()} size="sm" />,
      },
      {
        accessorKey: "r_multiple",
        header: () => (<span className="inline-flex items-center gap-1">R <InfoTip term="rMultiple" /></span>),
        cell: ({ getValue }) => {
          const v = getValue<number | null>();
          return <span className="tnum">{v != null ? `${fmtNum(v, 2)}R` : "—"}</span>;
        },
      },
      { accessorKey: "reason", header: "Reason", cell: ({ getValue }) => <Badge variant="outline" className="max-w-[160px] truncate">{getValue<string>() || "—"}</Badge> },
      { accessorKey: "mode", header: "Mode", cell: ({ getValue }) => <span className="text-xs text-muted-foreground">{getValue<string>()}</span> },
    ],
    [],
  );

  const table = useReactTable({
    data: rows,
    columns,
    state: { columnVisibility: visibility },
    onColumnVisibilityChange: setVisibility,
    getCoreRowModel: getCoreRowModel(),
  });

  const exportCsv = () => {
    const header = ["id", "closed_at", "symbol", "hold_hours", "entry", "exit", "return_pct", "pnl_usd", "r_multiple", "mode", "reason"];
    const lines = (data?.items ?? []).map((t) =>
      [t.id, t.closed_at ?? "", t.symbol, t.hold_hours ?? "", t.entry_price, t.exit_price ?? "", t.return_pct ?? "", t.pnl_usd ?? "", t.r_multiple ?? "", t.mode, `"${(t.reason ?? "").replace(/"/g, '""')}"`].join(","),
    );
    const blob = new Blob([[header.join(","), ...lines].join("\n")], { type: "text/csv" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `trades_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const agg = aggregates.data;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <CardTitle>Trade history</CardTitle>
            <div className="flex items-center gap-2">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button variant="outline" size="sm"><SlidersHorizontal className="size-3.5" /> Columns</Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent>
                  <DropdownMenuLabel>Toggle columns</DropdownMenuLabel>
                  {table.getAllLeafColumns().map((col) => (
                    <DropdownMenuCheckboxItem
                      key={col.id}
                      checked={col.getIsVisible()}
                      onCheckedChange={(v) => col.toggleVisibility(!!v)}
                    >
                      {col.id}
                    </DropdownMenuCheckboxItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>
              <Button variant="outline" size="sm" onClick={exportCsv}><Download className="size-3.5" /> CSV</Button>
            </div>
          </div>

          {/* Filters */}
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
            <div className="relative">
              <Search className="absolute left-2 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input className="pl-7" placeholder="Search coin / reason" value={search} onChange={(e) => setSearch(e.target.value)} aria-label="Search trades" />
            </div>
            <Input placeholder="Coin e.g. BTC/USDT" value={symbol} onChange={(e) => { setSymbol(e.target.value.toUpperCase()); setCursor(null); }} aria-label="Filter by symbol" />
            <div>
              <Label className="sr-only">From</Label>
              <Input type="date" value={dateFrom} onChange={(e) => { setDateFrom(e.target.value); setCursor(null); }} aria-label="From date" />
            </div>
            <div>
              <Label className="sr-only">To</Label>
              <Input type="date" value={dateTo} onChange={(e) => { setDateTo(e.target.value); setCursor(null); }} aria-label="To date" />
            </div>
          </div>
        </CardHeader>

        <CardContent>
          {isLoading ? (
            <Skeleton className="h-72" />
          ) : rows.length === 0 ? (
            <EmptyState title="No closed trades" description="Trades appear here once the bot opens and closes a position." />
          ) : (
            <div className="overflow-x-auto scrollbar-thin">
              <table className="w-full text-sm">
                <thead>
                  {table.getHeaderGroups().map((hg) => (
                    <tr key={hg.id} className="border-b border-border text-left text-xs text-muted-foreground">
                      {hg.headers.map((h) => (
                        <th key={h.id} className="whitespace-nowrap px-3 py-2 font-medium">
                          {h.isPlaceholder ? null : flexRender(h.column.columnDef.header, h.getContext())}
                        </th>
                      ))}
                    </tr>
                  ))}
                </thead>
                <tbody>
                  {table.getRowModel().rows.map((row) => (
                    <tr
                      key={row.id}
                      onClick={() => setOpenTrade(row.original.id)}
                      className="cursor-pointer border-b border-border/50 transition-colors hover:bg-accent/40 focus-within:bg-accent/40"
                      tabIndex={0}
                      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && setOpenTrade(row.original.id)}
                    >
                      {row.getVisibleCells().map((cell) => (
                        <td key={cell.id} className="whitespace-nowrap px-3 py-2.5">
                          {flexRender(cell.column.columnDef.cell, cell.getContext())}
                        </td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          {/* Footer aggregates + pagination */}
          <div className="mt-3 flex flex-wrap items-center justify-between gap-3 text-xs">
            <div className="flex flex-wrap items-center gap-3 text-muted-foreground">
              {agg && (
                <>
                  <span>{agg.count} trades</span>
                  <span>Net <PnLBadge usd={agg.total_pnl_usd} size="sm" /></span>
                  <span>{agg.win_rate_pct}% win · {agg.wins}W/{agg.losses}L</span>
                </>
              )}
            </div>
            <div className="flex items-center gap-2">
              <Button variant="outline" size="sm" disabled={cursor === null} onClick={() => setCursor(null)}>Newest</Button>
              <Button
                variant="outline"
                size="sm"
                disabled={!data?.has_more || isFetching}
                onClick={() => data?.next_cursor != null && setCursor(data.next_cursor)}
              >
                Older →
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <TradeDetailDialog id={openTrade} onClose={() => setOpenTrade(null)} />
    </div>
  );
}
