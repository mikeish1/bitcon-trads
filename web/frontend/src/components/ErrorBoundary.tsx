import { Component, type ErrorInfo, type ReactNode } from "react";
import { AlertTriangle } from "lucide-react";
import { Button } from "@/components/ui/button";

interface Props {
  children: ReactNode;
}
interface State {
  error: Error | null;
}

/** Catches render errors so one broken card never blanks the whole app. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error("UI error boundary:", error, info);
  }

  render(): ReactNode {
    if (this.state.error) {
      return (
        <div className="flex min-h-[40vh] flex-col items-center justify-center gap-3 text-center">
          <AlertTriangle className="size-8 text-warn" />
          <p className="text-sm font-medium">Something went wrong rendering this view.</p>
          <p className="max-w-md text-xs text-muted-foreground">{this.state.error.message}</p>
          <Button variant="outline" size="sm" onClick={() => this.setState({ error: null })}>
            Try again
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
