import { cn } from "@/lib/utils";

/** Shimmering placeholder used for first-load states (never a spinner). */
export function Skeleton({ className, ...props }: React.HTMLAttributes<HTMLDivElement>): JSX.Element {
  return (
    <div
      aria-hidden
      className={cn("animate-pulse rounded-md bg-muted/70", className)}
      {...props}
    />
  );
}
