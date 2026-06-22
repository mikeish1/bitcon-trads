import { Suspense } from "react";
import { Outlet } from "react-router-dom";
import { Sidebar, MobileTabs } from "@/components/layout/Sidebar";
import { Topbar } from "@/components/layout/Topbar";
import { Skeleton } from "@/components/ui/skeleton";

/** Root layout: fixed sidebar (desktop) / bottom tabs (mobile) + sticky topbar. */
export function AppShell(): JSX.Element {
  return (
    <div className="flex min-h-screen bg-background">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Topbar />
        <main className="flex-1 px-3 pb-20 pt-4 sm:px-5 md:pb-6">
          <div className="mx-auto w-full max-w-[1400px]">
            <Suspense fallback={<PageSkeleton />}>
              <Outlet />
            </Suspense>
          </div>
        </main>
      </div>
      <MobileTabs />
    </div>
  );
}

function PageSkeleton(): JSX.Element {
  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4 lg:grid-cols-6">
        {Array.from({ length: 6 }).map((_, i) => (
          <Skeleton key={i} className="h-20" />
        ))}
      </div>
      <Skeleton className="h-72" />
    </div>
  );
}
