import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";

/** shadcn className combiner: clsx + tailwind-merge. */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}

// --------------------------------------------------------------------------- //
// Formatting helpers (all locale-aware, tabular-friendly).                     //
// --------------------------------------------------------------------------- //
const USD = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 2,
});

const USD_PRECISE = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  maximumFractionDigits: 4,
});

export function fmtUsd(value: number | null | undefined, precise = false): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return (precise ? USD_PRECISE : USD).format(value);
}

/** Signed currency, e.g. "+$3.94" / "-$7.79". */
export function fmtUsdSigned(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : value < 0 ? "-" : "";
  return `${sign}${USD.format(Math.abs(value))}`;
}

export function fmtPct(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${value.toFixed(digits)}%`;
}

export function fmtPctSigned(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${value.toFixed(digits)}%`;
}

export function fmtNum(value: number | null | undefined, digits = 4): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return value.toLocaleString("en-US", { maximumFractionDigits: digits });
}

/** Compact relative duration, e.g. "3h", "2d 4h", "12m". */
export function fmtDuration(hours: number | null | undefined): string {
  if (hours === null || hours === undefined || Number.isNaN(hours)) return "—";
  if (hours < 1) return `${Math.round(hours * 60)}m`;
  if (hours < 24) return `${hours.toFixed(1)}h`;
  const d = Math.floor(hours / 24);
  const h = Math.round(hours % 24);
  return h ? `${d}d ${h}h` : `${d}d`;
}

export function fmtBytes(bytes: number): string {
  if (!bytes) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return `${(bytes / Math.pow(1024, i)).toFixed(i ? 1 : 0)} ${units[i]}`;
}

export function baseOf(symbol: string): string {
  return symbol.split("/")[0] ?? symbol;
}

/** Tailwind text color class for a signed value. */
export function pnlColor(value: number | null | undefined): string {
  if (value === null || value === undefined || value === 0) return "text-muted-foreground";
  return value > 0 ? "text-pos" : "text-neg";
}
