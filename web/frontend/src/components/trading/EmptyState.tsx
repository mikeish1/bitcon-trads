import { motion } from "framer-motion";
import { cn } from "@/lib/utils";

/**
 * Designed empty states — calm and explanatory, never an error. The bot legitimately
 * sits flat for days, so "no open positions" is a first-class, intentional state.
 */
interface EmptyStateProps {
  icon?: React.ReactNode;
  title: string;
  description?: string;
  action?: React.ReactNode;
  className?: string;
}

export function EmptyState({ icon, title, description, action, className }: EmptyStateProps): JSX.Element {
  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      className={cn(
        "flex flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-border/70 px-6 py-12 text-center",
        className,
      )}
    >
      {icon && <div className="text-muted-foreground/70">{icon}</div>}
      <p className="text-sm font-medium text-foreground">{title}</p>
      {description && <p className="max-w-sm text-xs text-muted-foreground">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </motion.div>
  );
}
