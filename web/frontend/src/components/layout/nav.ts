import {
  LayoutDashboard,
  Wallet,
  Layers,
  History,
  Brain,
  LineChart,
  ShieldAlert,
  Settings,
  Activity,
  BookOpen,
  type LucideIcon,
} from "lucide-react";

export interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

/** Primary navigation, one entry per domain (architecture §2). */
export const navItems: NavItem[] = [
  { to: "/", label: "Overview", icon: LayoutDashboard, end: true },
  { to: "/sleeves", label: "Sleeves", icon: Layers },
  { to: "/positions", label: "Positions", icon: Wallet },
  { to: "/history", label: "History", icon: History },
  { to: "/decisions", label: "Decisions", icon: Brain },
  { to: "/performance", label: "Performance", icon: LineChart },
  { to: "/risk", label: "Risk", icon: ShieldAlert },
  { to: "/config", label: "Config", icon: Settings },
  { to: "/health", label: "Health", icon: Activity },
  { to: "/strategy", label: "Strategy", icon: BookOpen },
];
