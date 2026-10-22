// HANSET sign-up — start Google OAuth. Cloudflare Pages Function (plain ES module, no deps).
// Runs server-side only: /join/ is a plain HTML form; the browser never talks to Google from our pages.
export async function onRequestGet({ request, env }) {
  if (!env.GOOGLE_CLIENT_ID) {
    return new Response("Sign-up is not open yet.", {
      status: 503,
      headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
    });
  }

  // Newsletter choice: the form sends nl=0 (hidden) then nl=1 (checkbox, pre-checked). Use the LAST value.
  const nlValues = new URL(request.url).searchParams.getAll("nl");
  const nl = nlValues[nlValues.length - 1] === "1" ? 1 : 0;

  const state = randomHex(32);
  const authUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  authUrl.searchParams.set("client_id", env.GOOGLE_CLIENT_ID);
  authUrl.searchParams.set("redirect_uri", env.GOOGLE_REDIRECT_URI || "https://hanset.soulbound.co.kr/api/auth/callback/google");
  authUrl.searchParams.set("response_type", "code");
  authUrl.searchParams.set("scope", "openid email profile");
  authUrl.searchParams.set("state", state);
  authUrl.searchParams.set("prompt", "select_account");

  return new Response(null, {
    status: 302,
    headers: {
      Location: authUrl.toString(),
      "Set-Cookie": `hs_oauth=${state}.${nl}; HttpOnly; Secure; SameSite=Lax; Path=/api/auth; Max-Age=600`,
      "Cache-Control": "no-store",
    },
  });
}

function randomHex(bytes) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}
