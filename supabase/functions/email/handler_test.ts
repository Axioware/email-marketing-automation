import { assert, assertEquals } from "jsr:@std/assert@1";
import { clientIp, createHandler, type FooterImage, RateLimiter, tokenFromPath, TRANSPARENT_GIF } from "./handler.ts";

const TOKEN = "A".repeat(20) + "b_c-" + "9".repeat(19); // 43 chars, like secrets.token_urlsafe(32)
const FOOTER: FooterImage = { body: new Uint8Array([1, 2, 3]), contentType: "image/png" };
const url = (path: string) => `https://ref.supabase.co/functions/v1${path}`;

function setup(
  opts: { footer?: FooterImage | null; recordFails?: boolean; footerThrows?: boolean; limiter?: RateLimiter } = {},
) {
  const calls: Array<[string, string]> = [];
  const handler = createHandler({
    limiter: opts.limiter,
    recordOpen: (token, ua) => {
      calls.push([token, ua]);
      return opts.recordFails ? Promise.reject(new Error("db down")) : Promise.resolve();
    },
    loadFooter: () => opts.footerThrows ? Promise.reject(new Error("storage down")) : Promise.resolve(opts.footer === undefined ? FOOTER : opts.footer),
  });
  return { handler, calls };
}

Deno.test("valid token records the open and returns the footer", async () => {
  const { handler, calls } = setup();
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`), { headers: { "user-agent": "Mail/1.0" } }));
  assertEquals(res.status, 200);
  assertEquals(res.headers.get("content-type"), "image/png");
  assertEquals(new Uint8Array(await res.arrayBuffer()), FOOTER.body);
  assertEquals(calls, [[TOKEN, "Mail/1.0"]]);
});

Deno.test("responses forbid caching", async () => {
  const { handler } = setup();
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`)));
  assert(res.headers.get("cache-control")!.includes("no-store"));
  await res.body?.cancel();
});

Deno.test("image extensions and trailing slash are accepted", async () => {
  for (const suffix of [".png", ".gif", ".jpg", "/"]) {
    const { handler, calls } = setup();
    const res = await handler(new Request(url(`/email/footer/${TOKEN}${suffix}`)));
    assertEquals(res.status, 200, suffix);
    assertEquals(calls.length, 1, suffix);
    await res.body?.cancel();
  }
});

Deno.test("invalid or malformed tokens still get the image but record nothing", async () => {
  for (const bad of ["short", "x".repeat(129), "has%20space" + "a".repeat(30), "a".repeat(40) + "%2F..", "%E0%A4%A"]) {
    const { handler, calls } = setup();
    const res = await handler(new Request(url(`/email/footer/${bad}`)));
    assertEquals(res.status, 200, bad);
    assertEquals(calls.length, 0, bad);
    assertEquals(new Uint8Array(await res.arrayBuffer()), FOOTER.body);
  }
});

Deno.test("unknown paths are 404 and record nothing", async () => {
  for (const path of ["/email", "/email/", "/email/header/" + TOKEN, "/email/footer/"]) {
    const { handler, calls } = setup();
    const res = await handler(new Request(url(path)));
    assertEquals(res.status, 404, path);
    assertEquals(calls.length, 0);
    await res.body?.cancel();
  }
});

Deno.test("HEAD returns headers only and does not count as an open", async () => {
  const { handler, calls } = setup();
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`), { method: "HEAD" }));
  assertEquals(res.status, 200);
  assertEquals(res.body, null);
  assertEquals(calls.length, 0);
});

Deno.test("other methods are rejected", async () => {
  const { handler, calls } = setup();
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`), { method: "POST", body: "x" }));
  assertEquals(res.status, 405);
  assertEquals(calls.length, 0);
  await res.body?.cancel();
});

Deno.test("a failing database still returns the image", async () => {
  const { handler, calls } = setup({ recordFails: true });
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`)));
  assertEquals(res.status, 200);
  assertEquals(calls.length, 1);
  assertEquals(new Uint8Array(await res.arrayBuffer()), FOOTER.body);
});

Deno.test("missing or failing footer falls back to a transparent gif", async () => {
  for (const opts of [{ footer: null }, { footerThrows: true }]) {
    const { handler } = setup(opts);
    const res = await handler(new Request(url(`/email/footer/${TOKEN}`)));
    assertEquals(res.status, 200);
    assertEquals(res.headers.get("content-type"), "image/gif");
    assertEquals(new Uint8Array(await res.arrayBuffer()), TRANSPARENT_GIF.body);
  }
});

Deno.test("user agent is truncated", async () => {
  const { handler, calls } = setup();
  const res = await handler(new Request(url(`/email/footer/${TOKEN}`), { headers: { "user-agent": "u".repeat(2000) } }));
  await res.body?.cancel();
  assertEquals(calls[0][1].length, 500);
});

Deno.test("tokenFromPath", () => {
  assertEquals(tokenFromPath(`/email/footer/${TOKEN}`), TOKEN);
  assertEquals(tokenFromPath(`/footer/${TOKEN}.png`), TOKEN);
  assertEquals(tokenFromPath("/email/footer/abc"), null);
  assertEquals(tokenFromPath("/email/other/" + TOKEN), null);
});

Deno.test("rate limit per token: over the limit still gets the image but is not recorded", async () => {
  const { handler, calls } = setup({ limiter: new RateLimiter(1000, 3) });
  for (let i = 0; i < 5; i++) {
    const res = await handler(new Request(url(`/email/footer/${TOKEN}`), { headers: { "x-forwarded-for": `10.0.0.${i}` } }));
    assertEquals(res.status, 200);
    assertEquals(new Uint8Array(await res.arrayBuffer()), FOOTER.body);
  }
  assertEquals(calls.length, 3);
});

Deno.test("rate limit per IP across tokens", async () => {
  const { handler, calls } = setup({ limiter: new RateLimiter(2, 100) });
  for (let i = 0; i < 4; i++) {
    const token = TOKEN.slice(0, -1) + String(i);
    const res = await handler(new Request(url(`/email/footer/${token}`), { headers: { "x-forwarded-for": "1.2.3.4, 10.0.0.1" } }));
    await res.body?.cancel();
  }
  assertEquals(calls.length, 2);
});

Deno.test("rate limit window resets after a minute", () => {
  let now = 0;
  const limiter = new RateLimiter(1, 1, () => now);
  assert(limiter.allow("ip", "t"));
  assert(!limiter.allow("ip", "t"));
  now = 60_000;
  assert(limiter.allow("ip", "t"));
});

Deno.test("rate limiter memory stays bounded", () => {
  let now = 0;
  const limiter = new RateLimiter(100, 100, () => now, 50);
  for (let i = 0; i < 500; i++) limiter.allow(`ip${i}`, `t${i}`);
  // deno-lint-ignore no-explicit-any
  assert((limiter as any).counts.size <= 50);
});

Deno.test("client IP comes from the first forwarded address", () => {
  assertEquals(clientIp(new Request("https://x", { headers: { "x-forwarded-for": "1.2.3.4, 5.6.7.8" } })), "1.2.3.4");
  assertEquals(clientIp(new Request("https://x", { headers: { "x-real-ip": "9.9.9.9" } })), "9.9.9.9");
  assertEquals(clientIp(new Request("https://x")), "unknown");
});
