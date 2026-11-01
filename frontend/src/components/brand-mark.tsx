import Image from "next/image";

import { BRAND_LOGO, BRAND_LOGO_HEIGHT, BRAND_LOGO_WIDTH, BRAND_NAME } from "@/lib/brand";
import { cn } from "@/lib/utils";

/**
 * The official ClipWright Studio logo.
 *
 * Proportions are locked to the square source artwork (no stretching, no
 * recolouring, no CSS filters): the supplied icon already has a transparent
 * background and works on the dark surfaces used throughout the app. `size` is
 * the rendered edge length in pixels, so 24 → 24×24.
 */
export function BrandLogo({
  size = 24,
  className,
  priority = false,
  decorative = true,
  alt,
}: {
  size?: number;
  className?: string;
  priority?: boolean;
  /** Decorative marks sit next to a text wordmark and keep an empty alt. */
  decorative?: boolean;
  alt?: string;
}) {
  return (
    <Image
      src={BRAND_LOGO}
      alt={decorative ? "" : alt ?? `${BRAND_NAME} logo`}
      width={size}
      height={size}
      priority={priority}
      // The artwork is square; object-contain guards against any layout that
      // would otherwise squash it.
      className={cn("shrink-0 object-contain", className)}
      style={{ width: size, height: size }}
      aria-hidden={decorative || undefined}
      unoptimized
      data-brand-logo=""
      data-source-width={BRAND_LOGO_WIDTH}
      data-source-height={BRAND_LOGO_HEIGHT}
    />
  );
}

/** Logo + wordmark, used in headers, sidebars and auth screens. */
export function BrandWordmark({
  size = 24,
  className,
  textClassName,
  showName = true,
  priority = false,
}: {
  size?: number;
  className?: string;
  textClassName?: string;
  showName?: boolean;
  priority?: boolean;
}) {
  return (
    <span className={cn("inline-flex items-center gap-2", className)}>
      <BrandLogo size={size} priority={priority} decorative />
      {showName ? (
        <span className={cn("font-display font-bold tracking-tight", textClassName)}>
          {BRAND_NAME}
        </span>
      ) : null}
    </span>
  );
}
