// Minecraft log mailbox (Cloudflare Worker + D1). Deployed by the Discord bot: /mclog cloudflare setup
//
//   POST /ingest   the Minecraft plugin drops a batch of log lines here
//                  header  Authorization: Bearer <ingest key>
//                  body    {"lines": [{"t": <unix ms>, "l": "BLOCK_BREAK | Steve | STONE @ ..."}, ...]}
//   POST /fetch?after=<id>&limit=<n>
//                  the bot picks the batches up; everything up to <id> it already has is deleted
//                  header  Authorization: Bearer <bot key>
//   GET  /health   {"ok": true, "version": 1, "pending": <batches waiting>}
//
// Free plan: 100,000 requests per day, D1 100,000 written rows per day - plenty for a batch every
// few seconds. Batches the bot never picks up are deleted after KEEP_DAYS.

const VERSION = 1;
const MAX_BODY = 1_000_000;        // bytes per batch
const FETCH_BYTES = 2_000_000;     // bytes per pick-up
const KEEP_DAYS = 7;

function bearer(request) {
  const auth = request.headers.get("Authorization") || "";
  return auth.startsWith("Bearer ") ? auth.slice(7).trim() : "";
}

function same(a, b) {
  // constant time compare
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length || !a.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function json(data, status = 200) {
  return new Response(JSON.stringify(data), { status, headers: { "Content-Type": "application/json" } });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const db = env.DB;
    if (url.pathname === "/health") {
      const row = await db.prepare("SELECT COUNT(*) AS n FROM batches").first();
      return json({ ok: true, version: VERSION, pending: row ? row.n : 0 });
    }
    if (url.pathname === "/ingest" && request.method === "POST") {
      if (!same(bearer(request), env.INGEST_KEY)) return json({ error: "wrong key" }, 401);
      const body = await request.text();
      if (body.length > MAX_BODY) return json({ error: `batch too big (max ${MAX_BODY} bytes)` }, 413);
      if (!body.trim()) return json({ ok: true, stored: false });
      await db.prepare("INSERT INTO batches (received, body) VALUES (?, ?)").bind(Date.now(), body).run();
      if (Math.random() < 0.01) {
        await db.prepare("DELETE FROM batches WHERE received < ?").bind(Date.now() - KEEP_DAYS * 86400000).run();
      }
      return json({ ok: true, stored: true });
    }
    if (url.pathname === "/fetch" && request.method === "POST") {
      if (!same(bearer(request), env.BOT_KEY)) return json({ error: "wrong key" }, 401);
      const after = Math.max(0, parseInt(url.searchParams.get("after") || "0", 10) || 0);
      const limit = Math.min(Math.max(parseInt(url.searchParams.get("limit") || "100", 10) || 100, 1), 500);
      if (after > 0) await db.prepare("DELETE FROM batches WHERE id <= ?").bind(after).run();
      const { results } = await db.prepare("SELECT id, received, body FROM batches WHERE id > ? ORDER BY id LIMIT ?")
        .bind(after, limit).all();
      const batches = [];
      let size = 0;
      for (const row of results || []) {
        if (batches.length && size + row.body.length > FETCH_BYTES) break;
        batches.push(row);
        size += row.body.length;
      }
      const more = batches.length < (results || []).length || (results || []).length === limit;
      return json({ ok: true, version: VERSION, batches, more });
    }
    return json({ error: "not found" }, 404);
  },
};
