import { Info } from "lucide-react";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { glossary, type GlossaryEntry, type GlossaryKey } from "@/lib/glossary";
import { cn } from "@/lib/utils";

/**
 * The educational tooltip. Renders the plain-English explanation plus the precise
 * formula for a glossary term. Keyboard-focusable and screen-reader friendly.
 * Either pass a glossary `term` key, or explicit `plain`/`math` strings.
 */
interface InfoTipProps {
  term?: GlossaryKey;
  plain?: string;
  math?: string;
  className?: string;
  children?: React.ReactNode;
}

export function InfoTip({ term, plain, math, className, children }: InfoTipProps): JSX.Element {
  // Indexing with a dynamic key yields the union of entry shapes; annotate so the
  // optional `math` field is accessible (some glossary entries omit it).
  const entry: GlossaryEntry | undefined = term ? glossary[term] : undefined;
  const plainText = plain ?? entry?.plain ?? "";
  const mathText = math ?? entry?.math;
  const label = entry?.term ?? "More information";

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <button
          type="button"
          aria-label={`${label}: ${plainText}`}
          className={cn(
            "inline-flex items-center text-muted-foreground transition-colors hover:text-foreground",
            className,
          )}
        >
          {children ?? <Info className="size-3.5" aria-hidden />}
        </button>
      </TooltipTrigger>
      <TooltipContent>
        {entry?.term && <p className="mb-1 font-semibold text-foreground">{entry.term}</p>}
        <p className="leading-relaxed text-muted-foreground">{plainText}</p>
        {mathText && (
          <p className="mt-2 rounded bg-muted px-2 py-1 font-mono text-[11px] text-foreground">
            {mathText}
          </p>
        )}
      </TooltipContent>
    </Tooltip>
  );
}
