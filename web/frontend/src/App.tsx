import { lazy } from "react";
import { createBrowserRouter, RouterProvider } from "react-router-dom";
import { TooltipProvider } from "@/components/ui/tooltip";
import { AppShell } from "@/components/layout/AppShell";
import { ErrorBoundary } from "@/components/ErrorBoundary";

// Route-level code splitting: each page is its own chunk.
const Overview = lazy(() => import("@/pages/Overview"));
const Sleeves = lazy(() => import("@/pages/Sleeves"));
const Positions = lazy(() => import("@/pages/Positions"));
const History = lazy(() => import("@/pages/History"));
const Decisions = lazy(() => import("@/pages/Decisions"));
const Performance = lazy(() => import("@/pages/Performance"));
const Risk = lazy(() => import("@/pages/Risk"));
const Config = lazy(() => import("@/pages/Config"));
const Health = lazy(() => import("@/pages/Health"));
const Strategy = lazy(() => import("@/pages/Strategy"));

const router = createBrowserRouter([
  {
    path: "/",
    element: <AppShell />,
    children: [
      { index: true, element: <Overview /> },
      { path: "sleeves", element: <Sleeves /> },
      { path: "positions", element: <Positions /> },
      { path: "history", element: <History /> },
      { path: "decisions", element: <Decisions /> },
      { path: "performance", element: <Performance /> },
      { path: "risk", element: <Risk /> },
      { path: "config", element: <Config /> },
      { path: "health", element: <Health /> },
      { path: "strategy", element: <Strategy /> },
    ],
  },
]);

export function App(): JSX.Element {
  return (
    <TooltipProvider delayDuration={150} skipDelayDuration={300}>
      <ErrorBoundary>
        <RouterProvider router={router} />
      </ErrorBoundary>
    </TooltipProvider>
  );
}
