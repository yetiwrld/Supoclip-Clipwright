"use client";

import { ThemeProvider as NextThemesProvider } from "next-themes";

/**
 * ClipWright Studio is dark-first: every route — landing page, auth, app,
 * editor, share pages — renders on the dark palette, and the choice is
 * persisted so it never flips back to white after a restart.
 *
 * The `.light` palette still exists behind the theme toggle, but it is not the
 * default and no route is forced into it.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      enableColorScheme={false}
      disableTransitionOnChange
      storageKey="clipwright-theme"
      themes={["dark", "light"]}
    >
      {children}
    </NextThemesProvider>
  );
}
