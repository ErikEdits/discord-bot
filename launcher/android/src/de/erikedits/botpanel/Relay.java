package de.erikedits.botpanel;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.SecureRandom;
import java.util.Base64;
import java.util.List;
import java.util.Map;
import javax.crypto.Mac;
import javax.crypto.spec.SecretKeySpec;

/**
 * Talks to the bot through Discord, exactly like the PC launcher (launcher/panel_launcher.py):
 * the request goes into the hidden relay channel through the webhook, the bot edits its answer
 * into that message, and we read it back. Wire format: see cogs/panel_launcher.py.
 */
final class Relay {
    static final int VERSION = 1;
    static final String PREFIX = "panel-relay:1";
    static final String USER_AGENT = "DiscordBot (https://github.com/ErikEdits/discord-bot, 1) PanelApp";
    static final long ANSWER_TIMEOUT_MS = 45_000;
    static final int MAX_BODY = 8 * 1024 * 1024;

    static final class RelayException extends Exception {
        RelayException(String message) {
            super(message);
        }
    }

    static final class Answer {
        final int status;
        final String contentType;
        final String location;
        final byte[] body;

        Answer(int status, String contentType, String location, byte[] body) {
            this.status = status;
            this.contentType = contentType;
            this.location = location;
            this.body = body;
        }

        String text() {
            return new String(body, StandardCharsets.UTF_8);
        }
    }

    private static final class Response {
        int status;
        byte[] body;
        HttpURLConnection conn;
    }

    private static final SecureRandom RANDOM = new SecureRandom();

    final String webhook;
    final String keyId;
    private final byte[] secret;
    private volatile long offsetMs;

    Relay(String webhook, String keyId, byte[] secret) {
        this.webhook = webhook;
        this.keyId = keyId;
        this.secret = secret;
    }

    // -------- Discord -------------------------------------------------------

    private Response discord(String method, String url, byte[] data, String contentType) throws RelayException {
        for (int attempt = 1; attempt <= 8; attempt++) {
            Response r = new Response();
            try {
                HttpURLConnection conn = (HttpURLConnection) new URL(url).openConnection();
                conn.setRequestMethod(method);
                conn.setConnectTimeout(30_000);
                conn.setReadTimeout(30_000);
                conn.setUseCaches(false);
                conn.setInstanceFollowRedirects(true);
                conn.setRequestProperty("User-Agent", USER_AGENT);
                if (data != null) {
                    conn.setDoOutput(true);
                    conn.setRequestProperty("Content-Type", contentType);
                    conn.setFixedLengthStreamingMode(data.length);
                    try (OutputStream out = conn.getOutputStream()) {
                        out.write(data);
                    }
                }
                r.status = conn.getResponseCode();
                InputStream in = r.status >= 400 ? conn.getErrorStream() : conn.getInputStream();
                r.body = in == null ? new byte[0] : readAll(in);
                r.conn = conn;
            } catch (IOException e) {
                if (attempt >= 3) throw new RelayException("Can't reach Discord: " + e.getMessage());
                sleep(500L * attempt);
                continue;
            }
            long date = r.conn.getHeaderFieldDate("Date", 0);
            if (date > 0) offsetMs = date - System.currentTimeMillis();
            if (r.status == 429) {
                double wait = 1.0;
                try {
                    wait = Double.parseDouble(Json.str(Json.object(utf8(r.body)), "retry_after"));
                } catch (RuntimeException ignored) {
                }
                sleep((long) (Math.min(Math.max(wait, 0.05), 30) * 1000) + 50);
                continue;
            }
            if (r.status >= 500) {
                sleep(500L * attempt);
                continue;
            }
            if ("0".equals(r.conn.getHeaderField("X-RateLimit-Remaining"))) {
                try {
                    sleep((long) (Math.min(Double.parseDouble(r.conn.getHeaderField("X-RateLimit-Reset-After")), 30) * 1000));
                } catch (RuntimeException ignored) {
                }
            }
            return r;
        }
        throw new RelayException("Discord is busy (rate limited) - try again in a moment.");
    }

    /** False when the webhook is gone (relay reset) - the setup code then no longer works. */
    boolean webhookExists() throws RelayException {
        int status = discord("GET", webhook, null, null).status;
        return status != 401 && status != 404;
    }

    Answer send(String method, String path, String contentType, byte[] body) throws RelayException {
        String nonce = hex(randomBytes(12));
        long t = (System.currentTimeMillis() + offsetMs) / 1000;
        String bodyHash = hex(sha256(body));
        String signed = String.join("\n", keyId, nonce, Long.toString(t), method, path, contentType, bodyHash);
        String header = "{\"v\":" + VERSION + ",\"k\":" + Json.quote(keyId) + ",\"n\":" + Json.quote(nonce)
                + ",\"t\":" + t + ",\"m\":" + Json.quote(method) + ",\"p\":" + Json.quote(path)
                + ",\"c\":" + Json.quote(contentType) + ",\"b\":" + Json.quote(bodyHash)
                + ",\"s\":" + Json.quote(hex(hmac(signed))) + "}";
        String content = PREFIX + " req " + header;
        if (content.length() > 2000) {
            return new Answer(414, "text/plain", "", utf8Bytes("Address too long for the relay."));
        }
        String payload = "{\"content\":" + Json.quote(content) + ",\"flags\":4,\"allowed_mentions\":{\"parse\":[]}"
                + (body.length > 0 ? ",\"attachments\":[{\"id\":0,\"filename\":\"body.bin\"}]}" : "}");
        Response r;
        if (body.length > 0) {
            String boundary = "----panel" + hex(randomBytes(12));
            ByteArrayOutputStream data = new ByteArrayOutputStream(body.length + 1024);
            write(data, "--" + boundary + "\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                    + "Content-Type: application/json\r\n\r\n" + payload + "\r\n");
            write(data, "--" + boundary + "\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"body.bin\"\r\n"
                    + "Content-Type: application/octet-stream\r\n\r\n");
            data.write(body, 0, body.length);
            write(data, "\r\n--" + boundary + "--\r\n");
            r = discord("POST", webhook + "?wait=true", data.toByteArray(), "multipart/form-data; boundary=" + boundary);
        } else {
            r = discord("POST", webhook + "?wait=true", utf8Bytes(payload), "application/json");
        }
        if (r.status == 401 || r.status == 404) {
            throw new RelayException("The relay webhook is gone - get a new setup code with /panel-launcher get.");
        }
        if (r.status != 200) {
            String text = utf8(r.body);
            throw new RelayException("Discord refused the request (HTTP " + r.status + "): "
                    + text.substring(0, Math.min(200, text.length())));
        }
        String messageId = Json.str(Json.object(utf8(r.body)), "id");
        long deadline = System.currentTimeMillis() + ANSWER_TIMEOUT_MS;
        long delay = 300;
        while (System.currentTimeMillis() < deadline) {
            sleep(delay);
            delay = Math.min(delay + 100, 1000);
            r = discord("GET", webhook + "/messages/" + messageId, null, null);
            if (r.status == 404) throw new RelayException("The request disappeared from the relay channel.");
            if (r.status != 200) continue;
            Map<String, Object> msg = Json.object(utf8(r.body));
            String text = Json.str(msg, "content");
            if (!text.startsWith(PREFIX + " res ")) continue;
            Map<String, Object> answer = Json.object(text.substring(PREFIX.length() + 5));
            if (!nonce.equals(Json.str(answer, "n"))) continue;
            byte[] data = new byte[0];
            Object attachments = msg.get("attachments");
            if (attachments instanceof List && !((List<?>) attachments).isEmpty()) {
                @SuppressWarnings("unchecked")
                Map<String, Object> first = (Map<String, Object>) ((List<?>) attachments).get(0);
                Response file = discord("GET", Json.str(first, "url"), null, null);
                if (file.status != 200) throw new RelayException("Couldn't download the answer (HTTP " + file.status + ").");
                data = file.body;
            }
            int status;
            try {
                status = Integer.parseInt(Json.str(answer, "s"));
            } catch (NumberFormatException e) {
                status = 502;
            }
            return new Answer(status, Json.str(answer, "c"), Json.str(answer, "l"), data);
        }
        throw new RelayException("The bot didn't answer in time - is it online?");
    }

    // -------- Helpers -------------------------------------------------------

    private byte[] hmac(String text) {
        try {
            Mac mac = Mac.getInstance("HmacSHA256");
            mac.init(new SecretKeySpec(secret, "HmacSHA256"));
            return mac.doFinal(utf8Bytes(text));
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static byte[] sha256(byte[] data) {
        try {
            return MessageDigest.getInstance("SHA-256").digest(data);
        } catch (Exception e) {
            throw new IllegalStateException(e);
        }
    }

    static byte[] randomBytes(int n) {
        byte[] out = new byte[n];
        RANDOM.nextBytes(out);
        return out;
    }

    static String hex(byte[] data) {
        StringBuilder out = new StringBuilder(data.length * 2);
        for (byte b : data) out.append(String.format("%02x", b & 0xff));
        return out.toString();
    }

    static byte[] base64(String text) {
        return Base64.getDecoder().decode(text);
    }

    static byte[] readAll(InputStream in) throws IOException {
        try (InputStream input = in) {
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[16384];
            int n;
            while ((n = input.read(buf)) > 0) out.write(buf, 0, n);
            return out.toByteArray();
        }
    }

    static String utf8(byte[] data) {
        return new String(data, StandardCharsets.UTF_8);
    }

    static byte[] utf8Bytes(String text) {
        return text.getBytes(StandardCharsets.UTF_8);
    }

    private static void write(ByteArrayOutputStream out, String text) {
        byte[] b = utf8Bytes(text);
        out.write(b, 0, b.length);
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
