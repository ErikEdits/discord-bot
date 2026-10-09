// Sends the plugin's log lines to the Cloudflare mailbox of the ErikEdits Discord bot.
// No Discord limit: lines are collected and sent in batches every few seconds.
//
// 1. Put this file into your plugin (change the package line to your package).
// 2. config.yml:
//        cloudflare:
//          enabled: true
//          url: "https://mclog-mailbox.<your-subdomain>.workers.dev/ingest"   # from /mclog cloudflare setup
//          key: "<ingest key>"                                                # from /mclog cloudflare setup
//          interval-seconds: 2
// 3. In onEnable():
//        if (getConfig().getBoolean("cloudflare.enabled")) {
//            cloudflare = new CloudflareLogSender(getConfig().getString("cloudflare.url"),
//                    getConfig().getString("cloudflare.key"), getConfig().getInt("cloudflare.interval-seconds", 2),
//                    getLogger());
//            cloudflare.start();
//        }
//    Everywhere you build a log line for the Discord webhook, also call:
//        if (cloudflare != null) cloudflare.log("BLOCK_BREAK | " + player.getName() + " | " + ...);
//    In onDisable():
//        if (cloudflare != null) cloudflare.shutdown();
//
// log() is thread-safe and cheap (no network on the server thread). Needs Java 11+ (Paper: 17/21).

package de.erikedits.uptimemanager; // <- your package

import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ConcurrentLinkedQueue;
import java.util.concurrent.Executors;
import java.util.concurrent.ScheduledExecutorService;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.logging.Level;
import java.util.logging.Logger;

public final class CloudflareLogSender {
    private static final int MAX_QUEUE = 200_000;          // lines kept while Cloudflare is not reachable
    private static final int MAX_BATCH_BYTES = 800_000;    // the mailbox takes up to 1 MB per batch

    private final URI url;
    private final String key;
    private final int intervalSeconds;
    private final Logger logger;
    private final HttpClient http = HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(10)).build();
    private final ConcurrentLinkedQueue<String> queue = new ConcurrentLinkedQueue<>();
    private final AtomicInteger queued = new AtomicInteger();
    private final AtomicInteger dropped = new AtomicInteger();
    private final ScheduledExecutorService timer = Executors.newSingleThreadScheduledExecutor(r -> {
        Thread t = new Thread(r, "cloudflare-log-sender");
        t.setDaemon(true);
        return t;
    });
    private List<String> retry = new ArrayList<>();
    private int failures;
    private long nextTry;

    public CloudflareLogSender(String url, String key, int intervalSeconds, Logger logger) {
        this.url = URI.create(url.trim());
        this.key = key.trim();
        this.intervalSeconds = Math.max(1, intervalSeconds);
        this.logger = logger;
    }

    public void start() {
        timer.scheduleWithFixedDelay(this::flushSafely, intervalSeconds, intervalSeconds, TimeUnit.SECONDS);
    }

    /** Queue one log line, e.g. "BLOCK_BREAK | Steve | STONE @ Location{...}". */
    public void log(String line) {
        if (line == null || line.isEmpty()) return;
        if (queued.incrementAndGet() > MAX_QUEUE) {          // Cloudflare down for a long time: drop the oldest
            if (queue.poll() != null) queued.decrementAndGet();
            dropped.incrementAndGet();
        }
        queue.add("{\"t\":" + System.currentTimeMillis() + ",\"l\":" + quote(line) + "}");
    }

    /** Send what's left (call in onDisable). */
    public void shutdown() {
        timer.shutdown();
        try {
            timer.awaitTermination(5, TimeUnit.SECONDS);
        } catch (InterruptedException ignored) {
            Thread.currentThread().interrupt();
        }
        nextTry = 0;
        for (int i = 0; i < 5 && (!retry.isEmpty() || !queue.isEmpty()); i++) {
            if (!flush()) break;
        }
    }

    private void flushSafely() {
        try {
            flush();
        } catch (Throwable t) {
            logger.log(Level.WARNING, "Cloudflare log sender: " + t, t);
        }
    }

    /** Sends one batch; returns false if it failed. */
    private synchronized boolean flush() {
        if (System.currentTimeMillis() < nextTry) return true;
        List<String> batch = retry;
        int size = 0;
        for (String item : batch) size += item.length() + 1;
        String item;
        while (size < MAX_BATCH_BYTES && (item = queue.poll()) != null) {
            queued.decrementAndGet();
            batch.add(item);
            size += item.length() + 1;
        }
        if (batch.isEmpty()) return true;
        String body = "{\"lines\":[" + String.join(",", batch) + "]}";
        try {
            HttpRequest request = HttpRequest.newBuilder(url)
                    .timeout(Duration.ofSeconds(20))
                    .header("Authorization", "Bearer " + key)
                    .header("Content-Type", "application/json")
                    .POST(HttpRequest.BodyPublishers.ofString(body, StandardCharsets.UTF_8))
                    .build();
            HttpResponse<String> response = http.send(request, HttpResponse.BodyHandlers.ofString());
            int code = response.statusCode();
            if (code == 200) {
                retry = new ArrayList<>();
                if (failures > 0) logger.info("Cloudflare log sender: reachable again, " + batch.size() + " lines sent");
                failures = 0;
                int lost = dropped.getAndSet(0);
                if (lost > 0) logger.warning("Cloudflare log sender: " + lost + " lines were dropped while it was down");
                return true;
            }
            if (code == 401) {
                logger.severe("Cloudflare log sender: wrong key - check cloudflare.key in config.yml");
            } else if (code == 413) {
                logger.warning("Cloudflare log sender: batch too big, sending it in halves");
                int half = batch.size() / 2;
                if (half > 0) {
                    retry = new ArrayList<>(batch.subList(0, half));
                    List<String> rest = new ArrayList<>(batch.subList(half, batch.size()));
                    for (String s : rest) { queue.add(s); queued.incrementAndGet(); }   // (order of the rest changes)
                    return false;
                }
                retry = new ArrayList<>();                                       // a single giant line: skip it
                return false;
            } else {
                logger.warning("Cloudflare log sender: HTTP " + code + " " + response.body());
            }
        } catch (Exception e) {
            if (failures == 0) logger.warning("Cloudflare log sender: not reachable (" + e + ") - retrying");
        }
        retry = batch;
        failures++;
        nextTry = System.currentTimeMillis() + Math.min(60_000L, 2_000L * failures);   // 2 s, 4 s ... max 1 min
        return false;
    }

    private static String quote(String s) {
        StringBuilder b = new StringBuilder(s.length() + 16).append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': b.append("\\\""); break;
                case '\\': b.append("\\\\"); break;
                case '\n': b.append("\\n"); break;
                case '\r': b.append("\\r"); break;
                case '\t': b.append("\\t"); break;
                default:
                    if (c < 0x20) b.append(String.format("\\u%04x", (int) c));
                    else b.append(c);
            }
        }
        return b.append('"').toString();
    }
}
