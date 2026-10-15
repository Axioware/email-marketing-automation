// Supabase Edge Function "email": open tracking via the email footer image.
// URL: https://<project>.supabase.co/functions/v1/email/footer/{tracking_token}
//
// Deploy without JWT verification (email clients send no auth header); supabase/config.toml sets this.
// Optional secrets: FOOTER_BUCKET (default "email-assets"), FOOTER_PATH (default "footer.png").
import { createClient } from "jsr:@supabase/supabase-js@2";
import { createHandler, type FooterImage } from "./handler.ts";

const supabase = createClient(
  Deno.env.get("SUPABASE_URL")!,
  Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!,
  { auth: { persistSession: false } },
);
const BUCKET = Deno.env.get("FOOTER_BUCKET") ?? "email-assets";
const PATH = Deno.env.get("FOOTER_PATH") ?? "footer.png";
const FOOTER_CACHE_MS = 10 * 60 * 1000;

let cached: { image: FooterImage; at: number } | null = null;

async function loadFooter(): Promise<FooterImage | null> {
  if (cached && Date.now() - cached.at < FOOTER_CACHE_MS) return cached.image;
  const { data, error } = await supabase.storage.from(BUCKET).download(PATH);
  if (error || !data) {
    console.error(`footer ${BUCKET}/${PATH} not found:`, error?.message);
    return null;
  }
  const image = { body: new Uint8Array(await data.arrayBuffer()), contentType: data.type || "image/png" };
  cached = { image, at: Date.now() };
  return image;
}

async function recordOpen(token: string, userAgent: string): Promise<void> {
  const { error } = await supabase.rpc("record_email_open", { p_token: token, p_user_agent: userAgent });
  if (error) throw new Error(error.message);
}

Deno.serve(createHandler({ recordOpen, loadFooter }));
