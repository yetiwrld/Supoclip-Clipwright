import type { Metadata } from "next";
import { NextIntlClientProvider } from "next-intl";
import { getLocale, getMessages, getTranslations } from "next-intl/server";
import { Geist, Geist_Mono, Syne } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import { AttributionTracker } from "@/components/attribution-tracker";
import { DataFastIdentity } from "@/components/datafast-identity";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { FeedbackButton } from "@/components/feedback-button";
import { PostHogIdentity } from "@/components/posthog-identity";
import { ThemeProvider } from "@/components/theme-provider";
import { intlLocale } from "@/i18n/config";
import { ANALYTICS_ATTR } from "@/lib/analytics-gate";
import { analyticsDecision, isOfflineMode } from "@/lib/offline-mode";
import { getSiteUrl } from "@/lib/site";
import { BRAND_FAVICON, BRAND_ICON_180, BRAND_ICON_192, BRAND_ICON_32, BRAND_ICON_512, BRAND_NAME } from "@/lib/brand";

const geistSans = Geist({
  variable: "--font-geist-sans",
  subsets: ["latin"],
});

const geistMono = Geist_Mono({
  variable: "--font-geist-mono",
  subsets: ["latin"],
});

const syne = Syne({
  variable: "--font-syne",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700", "800"],
});

const dataFastWebsiteId = process.env.NEXT_PUBLIC_DATAFAST_WEBSITE_ID;
const dataFastDomain = process.env.NEXT_PUBLIC_DATAFAST_DOMAIN;
const shouldTrackLocalhost = process.env.NEXT_PUBLIC_DATAFAST_ALLOW_LOCALHOST === "true";
// Tilly analytics runs next to DataFast; it only uses its own cookies and window.tilly.
const tillyWebsiteId = process.env.NEXT_PUBLIC_TILLY_WEBSITE_ID;

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("metadata");

  return {
  title: {
    default: t("title"),
    template: t("titleTemplate"),
  },
  description: t("description"),
  metadataBase: new URL(getSiteUrl()),
  applicationName: BRAND_NAME,
  authors: [{ name: `${BRAND_NAME} contributors`, url: getSiteUrl() }],
  creator: `${BRAND_NAME} contributors`,
  category: t("category"),
  manifest: "/manifest.webmanifest",
  icons: {
    icon: [
      { url: BRAND_FAVICON, sizes: "any" },
      { url: BRAND_ICON_32, sizes: "32x32", type: "image/png" },
      { url: BRAND_ICON_192, sizes: "192x192", type: "image/png" },
      { url: BRAND_ICON_512, sizes: "512x512", type: "image/png" },
    ],
    shortcut: BRAND_FAVICON,
    apple: [{ url: BRAND_ICON_180, sizes: "180x180", type: "image/png" }],
  },
  appleWebApp: {
    capable: true,
    title: BRAND_NAME,
    statusBarStyle: "black-translucent",
  },
  openGraph: {
    title: t("title"),
    description: t("openGraphDescription"),
    siteName: BRAND_NAME,
    type: "website",
  },
  twitter: {
    card: "summary_large_image",
    title: t("title"),
    description: t("twitterDescription"),
  },
  robots: {
    index: true,
    follow: true,
    googleBot: {
      index: true,
      follow: true,
      "max-image-preview": "large",
      "max-snippet": -1,
      "max-video-preview": -1,
    },
  },
  };
}

export default async function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  const locale = await getLocale();
  const messages = await getMessages();

  // Offline mode is a privacy setting, not just an AI switch: while it is on,
  // no analytics script is rendered, no goal is sent, no session is recorded and
  // errors stay on this machine. The decision is re-evaluated per request, so
  // flipping the switch in Settings takes effect on the next page load.
  const analytics = analyticsDecision();
  const offlineMode = isOfflineMode();
  const isDataFastEnabled = analytics.allowed && Boolean(dataFastWebsiteId && dataFastDomain);
  const isTillyEnabled = analytics.allowed && Boolean(tillyWebsiteId);

  // Dark is the product default: the class is present before hydration so
  // there is no white flash. next-themes keeps the user's saved choice.
  return (
    <html
      lang={intlLocale(locale)}
      className="dark"
      {...{ [ANALYTICS_ATTR]: analytics.allowed ? "on" : undefined }}
      data-offline-mode={offlineMode ? "on" : "off"}
      suppressHydrationWarning
    >
      <head>
        {isDataFastEnabled ? (
          <>
            <Script id="datafast-queue" strategy="beforeInteractive">
              {`window.datafast = window.datafast || function() {
  window.datafast.q = window.datafast.q || [];
  window.datafast.q.push(arguments);
};`}
            </Script>
            <Script
              id="datafast-script"
              strategy="afterInteractive"
              src="/js/script.js"
              data-website-id={dataFastWebsiteId}
              data-domain={dataFastDomain}
              data-allow-localhost={shouldTrackLocalhost ? "true" : undefined}
              data-disable-console="true"
            />
          </>
        ) : null}
        {isTillyEnabled ? (
          <>
            <Script id="tilly-queue" strategy="beforeInteractive">
              {`window.tilly = window.tilly || function() {
  window.tilly.q = window.tilly.q || [];
  window.tilly.q.push(arguments);
};`}
            </Script>
            <Script
              id="tilly-script"
              strategy="afterInteractive"
              src="https://tilly.samihindi.com/js/script.js"
              data-website-id={tillyWebsiteId}
              data-allow-localhost={shouldTrackLocalhost ? "true" : undefined}
            />
          </>
        ) : null}
      </head>
      <body className={`${geistSans.variable} ${geistMono.variable} ${syne.variable} bg-background text-foreground antialiased`}>
        <ThemeProvider>
          <NextIntlClientProvider locale={locale} messages={messages}>
            <TooltipProvider>
              {children}
              {analytics.allowed ? (
                <>
                  <DataFastIdentity />
                  <PostHogIdentity />
                </>
              ) : null}
              {/* First-party campaign storage; skipped entirely while offline. */}
              {offlineMode ? null : <AttributionTracker />}
              <FeedbackButton />
              <Toaster />
            </TooltipProvider>
          </NextIntlClientProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
