/* Contact form for bren.place/contact. The page posts here; this sends the message to the
   right inbox. The addresses live only in this file, never in the website. */
import { DurableObject } from "cloudflare:workers";

const TO = {
  metagame: { email: "brendan@metagame.games", label: "Metagame" },
  vibecamp: { email: "brendan@vibe.camp", label: "Vibecamp" },
  other: { email: "brendan.hurst+place@gmail.com", label: "bren.place" },
};
const FROM = { email: "contact@bren.place", name: "bren.place contact form" };
const PER_IP_PER_HOUR = 5;
const PER_DAY = 40;
const SITE = /^https:\/\/bren\.place$|^http:\/\/(localhost|127\.0\.0\.1)(:\d+)?$/;
const EMAIL = /^[^\s@<>()",;:]+@[^\s@<>()",;:]+\.[^\s@<>()",;:]+$/;

function cors(origin) {
  return {
    "Access-Control-Allow-Origin": origin,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
    Vary: "Origin",
  };
}
const oneLine = s => String(s || "").replace(/[\r\n\t]+/g, " ").trim();

export async function handleContact(req, env) {
  const origin = req.headers.get("Origin") || "";
  if (!SITE.test(origin)) return new Response("Forbidden", { status: 403 });
  const headers = cors(origin);
  const reply = (status, body) => new Response(JSON.stringify(body), { status, headers: { ...headers, "Content-Type": "application/json" } });
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers });
  if (req.method !== "POST") return reply(405, { ok: false, error: "Use the form to send a message." });

  let d;
  try { d = JSON.parse(await req.text()); } catch { return reply(400, { ok: false, error: "That message couldn't be read. Try again." }); }

  /* Bots fill in the hidden field and submit instantly. Pretend it worked so they move on. */
  if (d.website || !(Number(d.ms) > 2500)) return reply(200, { ok: true });

  const to = TO[d.topic] || TO.other;
  const name = oneLine(d.name).slice(0, 100);
  const email = oneLine(d.email).slice(0, 200);
  const message = String(d.message || "").trim();
  if (!name) return reply(400, { ok: false, error: "Add your name." });
  if (!EMAIL.test(email)) return reply(400, { ok: false, error: "Check your email address. It's how Brendan will reply." });
  if (message.length < 2) return reply(400, { ok: false, error: "Write a message." });
  if (message.length > 5000) return reply(400, { ok: false, error: "Keep the message under 5,000 characters." });

  const ip = req.headers.get("CF-Connecting-IP") || "unknown";
  const limiter = env.LIMITS.get(env.LIMITS.idFromName("contact"));
  const allowed = await limiter.take(ip);
  if (allowed === "ip") return reply(429, { ok: false, error: "That's a lot of messages. Try again in an hour." });
  if (allowed === "day") return reply(429, { ok: false, error: "The form is busy today. Try again tomorrow." });

  try {
    await env.EMAIL.send({
      to: to.email,
      from: FROM,
      replyTo: { email, name },
      subject: `[${to.label}] Message from ${name}`,
      text: `${message}\n\n---\nFrom: ${name} <${email}>\nTopic: ${to.label}\nSent with the contact form at bren.place/contact. Reply to this email to answer them.`,
    });
  } catch (e) {
    const code = String((e && e.code) || (e && e.name) || "unknown").slice(0, 60);
    const detail = String((e && e.message) || "").slice(0, 200);
    console.error("contact send failed", code, detail);
    return reply(502, { ok: false, error: "The message didn't send. Try again in a few minutes.", code, detail });
  }
  return reply(200, { ok: true });
}

/* Counts sends so the form can't be used to flood an inbox. */
export class ContactLimiter extends DurableObject {
  async take(ip) {
    const now = Date.now(), hour = Math.floor(now / 3600000), day = Math.floor(now / 86400000);
    const s = (await this.ctx.storage.get("s")) || { day, total: 0, hour, ips: {} };
    if (s.day !== day) { s.day = day; s.total = 0; }
    if (s.hour !== hour) { s.hour = hour; s.ips = {}; }
    if ((s.ips[ip] || 0) >= PER_IP_PER_HOUR) return "ip";
    if (s.total >= PER_DAY) return "day";
    s.ips[ip] = (s.ips[ip] || 0) + 1;
    s.total++;
    await this.ctx.storage.put("s", s);
    return "ok";
  }
}
