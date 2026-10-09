# Minecraft logs through Cloudflare

Discord lets a webhook post only about 30 messages per minute. With this, the Minecraft plugin
sends its log lines to a small free Cloudflare Worker ("mailbox") and the bot picks them up every
5 seconds - no Discord limit, nothing lost while the bot restarts (the mailbox keeps up to 7 days).

| File | What |
|---|---|
| `worker.js` | The mailbox. The bot uploads it itself (`/mclog cloudflare setup`). |
| `CloudflareLogSender.java` | Goes into the plugin: collects lines, sends a batch every 2 s, retries. |

Discord commands (admins):

- `/mclog cloudflare guide` - step by step: free Cloudflare account + API token
- `/mclog cloudflare setup` - paste the token, the bot creates D1 database + worker + workers.dev address
- `/mclog cloudflare plugin` - the Java file and `config.yml` lines with address and key filled in
- `/mclog cloudflare status` - is it working, batches waiting, last lines
- `/mclog cloudflare new-key` - new plugin key (old one stops working)
- `/mclog cloudflare off` / `on`

Token permissions: `Account · Workers Scripts · Edit`, `Account · D1 · Edit`, `Account · Account Settings · Read`.

The Discord webhook can stay as a stand-in: if no lines came through Cloudflare for 2 minutes,
the bot reads the Discord channel again; lines that later arrive through Cloudflare too are not
counted twice.

Request format (if you don't use the Java class): `POST <address>/ingest`,
`Authorization: Bearer <key>`, body `{"lines": [{"t": <unix ms>, "l": "BLOCK_BREAK | Steve | STONE @ ..."}]}`,
max. 1 MB per request.
