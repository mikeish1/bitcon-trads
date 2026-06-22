import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Toaster } from "sonner";
import { App } from "@/App";
import { ThemeProvider } from "@/hooks/useTheme";
import { StreamProvider } from "@/hooks/useStream";
import { ApiError, setAuthToken } from "@/lib/api";
import "@/index.css";

// Optional: if a token was provided at build time, seed it. In production prefer a
// runtime prompt so the token is never bundled; the backend is open when no
// DASHBOARD_TOKEN is configured server-side.
if (import.meta.env.VITE_DASHBOARD_TOKEN) {
  setAuthToken(import.meta.env.VITE_DASHBOARD_TOKEN);
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: true,
      retry: (failureCount, error) => {
        // Don't retry auth/permission errors; they won't fix themselves.
        if (error instanceof ApiError && [401, 403, 404].includes(error.status)) return false;
        return failureCount < 3;
      },
    },
  },
});

const root = document.getElementById("root");
if (!root) throw new Error("Root element #root not found");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <StreamProvider>
          <App />
          <Toaster
            theme="dark"
            position="bottom-right"
            toastOptions={{ className: "text-sm" }}
            closeButton
          />
        </StreamProvider>
      </ThemeProvider>
    </QueryClientProvider>
  </StrictMode>,
);
