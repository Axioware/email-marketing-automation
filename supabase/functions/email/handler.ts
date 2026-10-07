// Request handling for the email open-tracking endpoint, kept free of Supabase so it can be tested directly.
//
//   GET /email/footer/{tracking_token}
//
// Records the open (only for sent emails; the database function decides) and always answers with the footer
// image, whether or not the token is valid, so the response never reveals which tokens exist.

export type FooterImage = { body: Uint8Array<ArrayBuffer>; contentType: string };

export type Deps = {
  recordOpen: (token: string, userAgent: string) => Promise<void>;
  loadFooter: () => Promise<FooterImage | null>;
  limiter?: RateLimiter;
};

// Fixed-window counters per key, held in memory by each function instance. Requests over the limit still get the
// image but are not recorded. Supabase spreads requests across many instances, so this is only a light extra
// guard; the real per-email limit is enforced by the database function record_email_open.
export class RateLimiter {
  private counts = new Map<string, { windowStart: number; count: number }>();

  constructor(
    private readonly perIpPerMinute = 60,
    private readonly perTokenPerMinute = 10,
    private readonly now: () => number = Date.now,
    private readonly maxKeys = 10_000,
  ) {}

  allow(ip: string, token: string): boolean {
    const ipOk = this.hit(`ip:${ip}`, this.perIpPerMinute);
    const tokenOk = this.hit(`token:${token}`, this.perTokenPerMinute);
    return ipOk && tokenOk;
  }

  private hit(key: string, limit: number): boolean {
    const now = this.now();
    const entry = this.counts.get(key);
    if (!entry || now - entry.windowStart >= 60_000) {
      if (this.counts.size >= this.maxKeys) this.prune(now);
      this.counts.set(key, { windowStart: now, count: 1 });
      return true;
    }
    entry.count += 1;
    return entry.count <= limit;
  }

  private prune(now: number) {
    for (const [key, entry] of this.counts) {
      if (now - entry.windowStart >= 60_000) this.counts.delete(key);
    }
    if (this.counts.size >= this.maxKeys) this.counts.clear(); // all fresh: drop rather than grow without bound
  }
}

export function clientIp(req: Request): string {
  return req.headers.get("x-forwarded-for")?.split(",")[0].trim() || req.headers.get("x-real-ip") || "unknown";
}

// Must match TOKEN_PATTERN in scripts/generate_emails.py.
export const TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;
const PATH_PATTERN = /\/footer\/([^/]+?)(?:\.(?:png|gif|jpe?g|webp))?\/?$/;

// 1x1 transparent GIF, used when the footer image cannot be loaded so the email never shows a broken image.
export const TRANSPARENT_GIF: FooterImage = {
  body: Uint8Array.from(atob("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"), (c) => c.charCodeAt(0)),
  contentType: "image/gif",
};

const NO_CACHE = {
  "Cache-Control": "no-store, no-cache, must-revalidate, private, max-age=0",
  "Pragma": "no-cache",
  "Expires": "0",
};

export function tokenFromPath(pathname: string): string | null {
  const match = pathname.match(PATH_PATTERN);
  if (!match) return null;
  let token: string;
  try {
    token = decodeURIComponent(match[1]);
  } catch {
    return null;
  }
  return TOKEN_PATTERN.test(token) ? token : null;
}

export function createHandler(deps: Deps): (req: Request) => Promise<Response> {
  const limiter = deps.limiter ?? new RateLimiter();
  return async (req: Request) => {
    if (req.method !== "GET" && req.method !== "HEAD") {
      return new Response(null, { status: 405, headers: { Allow: "GET, HEAD" } });
    }
    const pathname = new URL(req.url).pathname;
    if (!PATH_PATTERN.test(pathname)) {
      return new Response("Not found", { status: 404 });
    }

    const token = tokenFromPath(pathname);
    if (token && req.method === "GET" && limiter.allow(clientIp(req), token)) {
      try {
        await deps.recordOpen(token, (req.headers.get("user-agent") ?? "").slice(0, 500));
      } catch (error) {
        console.error("record_email_open failed:", error); // tracking must never break the image
      }
    }

    let image: FooterImage | null = null;
    try {
      image = await deps.loadFooter();
    } catch (error) {
      console.error("footer image unavailable:", error);
    }
    image ??= TRANSPARENT_GIF;
    return new Response(req.method === "HEAD" ? null : image.body, {
      status: 200,
      headers: { "Content-Type": image.contentType, ...NO_CACHE },
    });
  };
}
