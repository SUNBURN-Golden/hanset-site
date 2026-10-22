// HANSET sign-up — OAuth callback: verify state, exchange the code, store the member, clear the cookie.
// Cloudflare Pages Function (plain ES module, no deps). Stores only what /privacy/ lists; never logs tokens or emails.
export async function onRequestGet({ request, env }) {
  try {
    if (!env.GOOGLE_CLIENT_ID || !env.GOOGLE_CLIENT_SECRET || !env.DB) throw new Error("not configured");

    const url = new URL(request.url);
    const code = url.searchParams.get("code");
    const state = url.searchParams.get("state") || "";
    const oauthError = url.searchParams.get("error");
    const cookie = cookieValue(request.headers.get("Cookie") || "", "hs_oauth");
    const dotAt = cookie ? cookie.indexOf(".") : -1;
    const cookieState = dotAt > 0 ? cookie.slice(0, dotAt) : "";
    const cookieNl = dotAt > 0 ? cookie.slice(dotAt + 1) : "";

    if (oauthError || !code || !cookieState || !sameString(state, cookieState) || (cookieNl !== "0" && cookieNl !== "1")) {
      return failure();
    }

    const tokenRes = await fetch("https://oauth2.googleapis.com/token", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({
        code,
        client_id: env.GOOGLE_CLIENT_ID,
        client_secret: env.GOOGLE_CLIENT_SECRET,
        redirect_uri: env.GOOGLE_REDIRECT_URI || "https://hanset.soulbound.co.kr/api/auth/callback/google",
        grant_type: "authorization_code",
      }),
    });
    const tokens = await tokenRes.json();
    if (!tokenRes.ok || !tokens.access_token) throw new Error("token exchange failed");

    const infoRes = await fetch("https://openidconnect.googleapis.com/v1/userinfo", {
      headers: { Authorization: "Bearer " + tokens.access_token },
    });
    const info = await infoRes.json();
    if (!infoRes.ok || info.email_verified !== true || !info.sub || !info.email) throw new Error("userinfo rejected");

    // Repeat login refreshes email/name only — the existing newsletter choice is never overwritten.
    await env.DB.prepare(
      "INSERT INTO members (google_sub,email,name,newsletter_opt_in,created_at) VALUES (?,?,?,?,?) " +
        "ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email, name=excluded.name"
    ).bind(info.sub, info.email, info.name || null, cookieNl === "1" ? 1 : 0, new Date().toISOString()).run();

    return redirect("/join/welcome/");
  } catch {
    return failure();
  }
}

function failure() {
  return redirect("/join/?error=1");
}
function redirect(location) {
  return new Response(null, {
    status: 302,
    headers: { Location: location, "Set-Cookie": clearCookie(), "Cache-Control": "no-store" },
  });
}
function clearCookie() {
  return "hs_oauth=; HttpOnly; Secure; SameSite=Lax; Path=/api/auth; Max-Age=0";
}
function cookieValue(header, name) {
  for (const part of header.split(";")) {
    const eq = part.indexOf("=");
    if (eq > -1 && part.slice(0, eq).trim() === name) return part.slice(eq + 1).trim();
  }
  return null;
}
// Constant-time-ish compare: equal length, XOR of every char code.
function sameString(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}
