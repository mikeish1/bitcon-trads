import { motion } from "framer-motion";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { InfoTip } from "@/components/trading/InfoTip";
import { cn } from "@/lib/utils";
import type { GlossaryKey } from "@/lib/glossary";

interface MetricCardProps {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  term?: GlossaryKey;
  tone?: "default" | "pos" | "neg" | "warn";
  loading?: boolean;
  className?: string;
}

const toneText: Record<NonNullable<MetricCardProps["tone"]>, string> = {
  default: "text-foreground",
  pos: "text-pos",
  neg: "text-neg",
  warn: "text-warn",
};

/** Compact KPI tile with optional glossary tooltip and a value roll-in. */
export function MetricCard({
  label,
  value,
  sub,
  term,
  tone = "default",
  loading,
  className,
}: MetricCardProps): JSX.Element {
  return (
    <Card className={cn("p-3 sm:p-4", className)}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </span>
        {term && <InfoTip term={term} />}
      </div>
      {loading ? (
        <Skeleton className="mt-2 h-7 w-24" />
      ) : (
        <motion.div
          initial={{ opacity: 0, y: 4 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.2 }}
          className={cn("mt-1 text-xl font-semibold tnum sm:text-2xl", toneText[tone])}
        >
          {value}
        </motion.div>
      )}
      {sub && !loading && <div className="mt-0.5 text-xs text-muted-foreground tnum">{sub}</div>}
    </Card>
  );
}
