import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider as NextThemesProvider } from "next-themes";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { ThemeProvider } from "./theme-provider";
import { ThemeToggle } from "./theme-toggle";
import { TooltipProvider } from "@/components/ui/tooltip";

const navigation = vi.hoisted(() => ({ pathname: "/" }));
vi.mock("next/navigation", () => ({ usePathname: () => navigation.pathname }));

let storage: Map<string, string>;

beforeEach(() => {
  storage = new Map();
  vi.stubGlobal("localStorage", {
    getItem: vi.fn((key: string) => storage.get(key) ?? null),
    setItem: vi.fn((key: string, value: string) => { storage.set(key, value); }),
    removeItem: vi.fn((key: string) => { storage.delete(key); }),
  });
  vi.stubGlobal("matchMedia", () => ({ matches: true, addListener() {}, removeListener() {}, addEventListener() {}, removeEventListener() {} }));
  vi.stubGlobal("ResizeObserver", class { observe() {} unobserve() {} disconnect() {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.documentElement.className = "";
  navigation.pathname = "/";
});

function renderToggle(defaultTheme: "light" | "dark") {
  return render(
    <NextThemesProvider attribute="class" defaultTheme={defaultTheme} enableSystem={false} storageKey="clipwright-theme">
      <TooltipProvider>
        <ThemeToggle />
      </TooltipProvider>
    </NextThemesProvider>,
  );
}

it("switches from dark to light and back", async () => {
  const user = userEvent.setup();
  renderToggle("dark");
  const toggle = screen.getByRole("button", { name: "Switch light/dark theme" });

  await user.click(toggle);
  expect(document.documentElement).toHaveClass("light");
  expect(storage.get("clipwright-theme")).toBe("light");

  await user.click(toggle);
  expect(document.documentElement).toHaveClass("dark");
  expect(storage.get("clipwright-theme")).toBe("dark");
});

it("starts dark on every route, including marketing and auth pages", () => {
  for (const pathname of ["/", "/list", "/sign-in", "/blog", "/tasks/abc", "/share/token"]) {
    document.documentElement.className = "";
    storage.clear();
    navigation.pathname = pathname;
    const view = render(<ThemeProvider><span /></ThemeProvider>);
    expect(document.documentElement, pathname).toHaveClass("dark");
    view.unmount();
  }
});

it("ignores a dark system preference only in the sense that dark is already the default", () => {
  navigation.pathname = "/list";
  render(<ThemeProvider><span /></ThemeProvider>);
  expect(document.documentElement).toHaveClass("dark");
});

it("keeps the saved light choice after a reload", () => {
  storage.set("clipwright-theme", "light");
  navigation.pathname = "/sign-in";
  render(<ThemeProvider><span /></ThemeProvider>);
  expect(document.documentElement).toHaveClass("light");
  expect(document.documentElement).not.toHaveClass("dark");
});

it("keeps the saved dark choice on auth routes", () => {
  storage.set("clipwright-theme", "dark");
  navigation.pathname = "/sign-in";
  render(<ThemeProvider><span /></ThemeProvider>);
  expect(document.documentElement).toHaveClass("dark");
});
