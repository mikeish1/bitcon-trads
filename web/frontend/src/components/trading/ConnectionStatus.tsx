import { useEffect, useState } from "react";
import { Wifi, WifiOff, RefreshCw, Radio } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useStreamStatus } from "@/hooks/useStream";
import { cn } from "@/lib/utils";

/** Live / polling / offline indicator + "updated Ns ago" derived from the stream. */
export function ConnectionStatus(): JSX.Element {
  const { state, lastEventAt } = useStreamStatus();
  const [, force] = useState(0);

  // Tick once a second so the "updated Ns ago" label stays current.
  useEffect(() => {
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, []);

  const ago = lastEventAt ? Math.round((Date.now() - lastEventAt) / 1000) : null;
  const map = {
    connecting: { icon: RefreshCw, color: "text-warn", label: "Connecting", spin: true },
    live: { icon: Radio, color: "text-pos", label: "Live", spin: false },
    polling: { icon: Wifi, color: "text-warn", label: "Polling", spin: false },
    offline: { icon: WifiOff, color: "text-neg", label: "Offline", spin: false },
  } as const;
  const s = map[state];
  const Icon = s.icon;

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className={cn("inline-flex items-center gap-1.5 text-xs font-medium", s.color)}>
          <Icon className={cn("size-3.5", s.spin && "animate-spin")} aria-hidden />
          <span className="hidden sm:inline">{s.label}</span>
          {ago !== null && state === "live" && (
            <span className="text-muted-foreground tnum">· {ago}s</span>
          )}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        {state === "live" && "Real-time stream connected (SSE). Updates push instantly."}
        {state === "polling" && "Stream unavailable — falling back to periodic polling. Data stays fresh."}
        {state === "connecting" && "Establishing the real-time stream…"}
        {state === "offline" && "No network connection."}
      </TooltipContent>
    </Tooltip>
  );
}
