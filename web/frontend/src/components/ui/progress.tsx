import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Accessible determinate progress bar (role=progressbar with aria values).
 * `tone` colors the fill; used by RiskGauge to reflect proximity to a limit.
 */
export interface ProgressProps extends React.HTMLAttributes<HTMLDivElement> {
  value: number; // 0..100
  tone?: "primary" | "pos" | "warn" | "neg" | "regime";
  label?: string;
}

const toneClass: Record<NonNullable<ProgressProps["tone"]>, string> = {
  primary: "bg-primary",
  pos: "bg-pos",
  warn: "bg-warn",
  neg: "bg-neg",
  regime: "bg-regime",
};

export const Progress = React.forwardRef<HTMLDivElement, ProgressProps>(
  ({ className, value, tone = "primary", label, ...props }, ref) => {
    const clamped = Math.max(0, Math.min(100, value));
    return (
      <div
        ref={ref}
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(clamped)}
        aria-label={label}
        className={cn("relative h-2 w-full overflow-hidden rounded-full bg-muted", className)}
        {...props}
      >
        <div
          className={cn("h-full rounded-full transition-[width] duration-500 ease-out", toneClass[tone])}
          style={{ width: `${clamped}%` }}
        />
      </div>
    );
  },
);
Progress.displayName = "Progress";
