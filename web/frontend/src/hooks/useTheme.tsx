import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

type Theme = "dark" | "light";
interface ThemeCtx {
  theme: Theme;
  toggle: () => void;
}
const Ctx = createContext<ThemeCtx>({ theme: "dark", toggle: () => {} });
const KEY = "bt-theme";

export function ThemeProvider({ children }: { children: ReactNode }): JSX.Element {
  const [theme, setTheme] = useState<Theme>(() => {
    const saved = localStorage.getItem(KEY);
    return saved === "light" ? "light" : "dark"; // dark is the default
  });

  useEffect(() => {
    const root = document.documentElement;
    root.classList.toggle("dark", theme === "dark");
    root.classList.toggle("light", theme === "light");
    localStorage.setItem(KEY, theme);
  }, [theme]);

  const toggle = useCallback(() => setTheme((t) => (t === "dark" ? "light" : "dark")), []);
  const value = useMemo(() => ({ theme, toggle }), [theme, toggle]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useTheme(): ThemeCtx {
  return useContext(Ctx);
}
