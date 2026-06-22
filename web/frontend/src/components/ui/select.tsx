import * as React from "react";
import { cn } from "@/lib/utils";

/**
 * Lightweight native <select> styled to match the theme. Used for filters where a
 * full Radix listbox would be overkill; keyboard + a11y come free from the native
 * element.
 */
export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  options: { value: string; label: string }[];
}

export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(
  ({ className, options, ...props }, ref) => (
    <select
      ref={ref}
      className={cn(
        "h-9 rounded-md border border-input bg-background px-2 text-sm text-foreground shadow-sm",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
        className,
      )}
      {...props}
    >
      {options.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  ),
);
Select.displayName = "Select";
