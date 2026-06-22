import { NavLink } from "react-router-dom";
import { CandlestickChart } from "lucide-react";
import { navItems } from "@/components/layout/nav";
import { cn } from "@/lib/utils";

/** Desktop left rail. Collapses to an icon-only strip on md, hidden on mobile
 * (mobile uses the bottom tab bar). */
export function Sidebar(): JSX.Element {
  return (
    <aside className="sticky top-0 hidden h-screen shrink-0 border-r border-border bg-card/40 md:flex md:w-16 md:flex-col lg:w-56">
      <div className="flex h-14 items-center gap-2 border-b border-border px-4">
        <CandlestickChart className="size-5 shrink-0 text-primary" aria-hidden />
        <span className="hidden font-semibold lg:inline">Bitcon-Trads</span>
      </div>
      <nav className="flex flex-1 flex-col gap-1 p-2" aria-label="Primary">
        {navItems.map(({ to, label, icon: Icon, end }) => (
          <NavLink
            key={to}
            to={to}
            end={end}
            className={({ isActive }) =>
              cn(
                "flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors",
                "hover:bg-accent hover:text-accent-foreground",
                isActive ? "bg-secondary text-foreground" : "text-muted-foreground",
              )
            }
          >
            <Icon className="size-4 shrink-0" aria-hidden />
            <span className="hidden lg:inline">{label}</span>
          </NavLink>
        ))}
      </nav>
      <div className="hidden border-t border-border p-3 text-[10px] text-muted-foreground lg:block">
        Read-only · monitoring
      </div>
    </aside>
  );
}

/** Mobile bottom tab bar (thumb-reachable; the 6 most-used domains). */
export function MobileTabs(): JSX.Element {
  const items = navItems.slice(0, 6);
  return (
    <nav
      className="fixed inset-x-0 bottom-0 z-40 flex border-t border-border bg-card/95 backdrop-blur md:hidden"
      aria-label="Primary mobile"
    >
      {items.map(({ to, label, icon: Icon, end }) => (
        <NavLink
          key={to}
          to={to}
          end={end}
          className={({ isActive }) =>
            cn(
              "flex flex-1 flex-col items-center gap-0.5 py-2 text-[10px] font-medium",
              isActive ? "text-primary" : "text-muted-foreground",
            )
          }
        >
          <Icon className="size-4" aria-hidden />
          {label}
        </NavLink>
      ))}
    </nav>
  );
}
