/**
 * Single source of truth for ClipWright Studio branding.
 *
 * The logo itself lives in one place — `public/brand/` — and every screen
 * imports these constants instead of hardcoding a path. The source artwork is
 * the supplied 1254×1254 PNG; the smaller sizes and the favicon are derived
 * from it without cropping, redrawing or recolouring.
 *
 * Licensing: this is a rebrand of the AGPL-3.0 SupoClip project. Copyright and
 * licence notices stay in LICENSE / README and must not be removed.
 */

export const BRAND_NAME = "ClipWright Studio";
export const BRAND_SHORT_NAME = "ClipWright";
/** Used where a sentence needs the product name in lower-case prose. */
export const BRAND_TAGLINE = "Your studio, entirely on your machine.";
export const BRAND_DESCRIPTION =
  "ClipWright Studio turns long videos into captioned shorts with local Whisper transcription and a local Ollama model — no API keys, no cloud, no uploads.";

/** Master artwork (1254×1254, transparent background, unchanged pixels). */
export const BRAND_LOGO = "/brand/clipwright-logo.png";
export const BRAND_LOGO_WIDTH = 1254;
export const BRAND_LOGO_HEIGHT = 1254;

/** Derived sizes of the same artwork, for icons and app metadata. */
export const BRAND_ICON_512 = "/brand/clipwright-icon-512.png";
export const BRAND_ICON_192 = "/brand/clipwright-icon-192.png";
export const BRAND_ICON_180 = "/brand/clipwright-icon-180.png";
export const BRAND_ICON_64 = "/brand/clipwright-icon-64.png";
export const BRAND_ICON_32 = "/brand/clipwright-icon-32.png";
export const BRAND_ICON_16 = "/brand/clipwright-icon-16.png";
export const BRAND_FAVICON = "/brand/favicon.ico";

/** Every brand asset lives here; nothing else in the app ships a logo copy. */
export const BRAND_ASSET_DIRECTORY = "/brand";
