export const COMPANY_NAME = "Shiori LLC";
export const COMPANY_REGISTRATION_NUMBER = "10603065";
export const COMPANY_REGISTERED_ADDRESS = "8 The Green, Suite B, Dover, DE 19901, United States";
export const SITE_NAME = "ClipWright Studio";
// Self-hosted by default: the app is opened on this machine, not on a hosted
// marketing site. Override with NEXT_PUBLIC_APP_URL.
export const DEFAULT_SITE_URL = "http://localhost:3107";
export const HOSTED_APP_URL = DEFAULT_SITE_URL;
// Upstream project attribution: required by AGPL-3.0 and kept on purpose.
export const GITHUB_URL = "https://github.com/FujiwaraChoki/supoclip";

/**
 * Creator codes are redeemed inside ClipWright Studio itself; there is no
 * hosted store to send people to. Returns an in-app deep link.
 */
export function creatorCodeRedeemUrl(code: string) {
  return `/settings/creator-program?code=${encodeURIComponent(code.toUpperCase())}`;
}

export function getSiteUrl() {
  try {
    return new URL(process.env.NEXT_PUBLIC_APP_URL || DEFAULT_SITE_URL).origin;
  } catch {
    return DEFAULT_SITE_URL;
  }
}
