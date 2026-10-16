package de.erikedits.botpanel;

import java.io.BufferedInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Locale;
import java.util.Map;
import java.util.concurrent.ConcurrentHashMap;
import java.util.concurrent.Semaphore;

/**
 * Small web server on 127.0.0.1 inside the app. The WebView talks to it, it forwards every
 * request through {@link Relay}. Same rules as the PC launcher: only Host 127.0.0.1/localhost
 * (no DNS rebinding), and a gate cookie so other apps on the phone can't use it.
 */
final class LocalServer {
    static final int FIRST_PORT = 8765;
    static final int LAST_PORT = 8799;

    interface Log {
        void line(String text);
    }

    private final Relay relay;
    private final Log log;
    final String gate = Relay.hex(Relay.randomBytes(24));
    private final Map<String, Object[]> staticCache = new ConcurrentHashMap<>();
    private final Semaphore slots = new Semaphore(3);
    private ServerSocket server;
    private volatile boolean running;
    int port;

    LocalServer(Relay relay, Log log) {
        this.relay = relay;
        this.log = log;
    }

    int start() throws IOException {
        InetAddress local = InetAddress.getByName("127.0.0.1");
        for (int p = FIRST_PORT; p <= LAST_PORT; p++) {
            try {
                server = new ServerSocket(p, 50, local);
                port = p;
                break;
            } catch (IOException ignored) {
            }
        }
        if (server == null) throw new IOException("No free port between " + FIRST_PORT + " and " + LAST_PORT);
        running = true;
        Thread accept = new Thread(this::acceptLoop, "panel-accept");
        accept.setDaemon(true);
        accept.start();
        return port;
    }

    String startUrl() {
        return "http://127.0.0.1:" + port + "/__launcher/start?t=" + gate;
    }

    void stop() {
        running = false;
        try {
            if (server != null) server.close();
        } catch (IOException ignored) {
        }
    }

    private void acceptLoop() {
        while (running) {
            try {
                Socket socket = server.accept();
                Thread t = new Thread(() -> handle(socket), "panel-request");
                t.setDaemon(true);
                t.start();
            } catch (IOException e) {
                if (!running) return;
            }
        }
    }

    // -------- One request ---------------------------------------------------

    private void handle(Socket socket) {
        try (Socket s = socket) {
            s.setSoTimeout(60_000);
            InputStream in = new BufferedInputStream(s.getInputStream());
            OutputStream out = s.getOutputStream();
            String head = readHead(in);
            if (head == null) return;
            String[] lines = head.split("\r\n");
            String[] first = lines[0].split(" ");
            if (first.length < 3) {
                reply(out, "GET", 400, "text/plain", Relay.utf8Bytes("Bad request"), null);
                return;
            }
            String method = first[0].toUpperCase(Locale.ROOT);
            String path = first[1];
            Map<String, String> headers = new HashMap<>();
            for (int k = 1; k < lines.length; k++) {
                int colon = lines[k].indexOf(':');
                if (colon > 0) {
                    headers.put(lines[k].substring(0, colon).trim().toLowerCase(Locale.ROOT), lines[k].substring(colon + 1).trim());
                }
            }
            String host = headers.getOrDefault("host", "");
            if (!host.equals("127.0.0.1:" + port) && !host.equals("localhost:" + port)) {
                reply(out, method, 403, "text/plain", Relay.utf8Bytes("Forbidden"), null);
                return;
            }
            if (path.startsWith("/__launcher/start")) {
                if (path.equals("/__launcher/start?t=" + gate)) {
                    Map<String, String> extra = new HashMap<>();
                    extra.put("Location", "/");
                    extra.put("Set-Cookie", "panel_gate=" + gate + "; Path=/; HttpOnly; SameSite=Strict");
                    reply(out, method, 303, "", new byte[0], extra);
                } else {
                    reply(out, method, 403, "text/html; charset=utf-8", page("Old link", "Restart the app."), null);
                }
                return;
            }
            boolean gated = false;
            for (String c : headers.getOrDefault("cookie", "").split(";")) {
                if (c.trim().equals("panel_gate=" + gate)) gated = true;
            }
            if (!gated) {
                reply(out, method, 403, "text/html; charset=utf-8",
                        page("Open the panel in the app", "This address only works inside the Bot Panel app."), null);
                return;
            }
            if (path.equals("/favicon.ico")) {
                reply(out, method, 204, "", new byte[0], null);
                return;
            }
            if (!method.equals("GET") && !method.equals("POST") && !method.equals("HEAD")) {
                reply(out, method, 405, "text/plain", Relay.utf8Bytes("Method not allowed"), null);
                return;
            }
            long length = 0;
            try {
                length = Long.parseLong(headers.getOrDefault("content-length", "0"));
            } catch (NumberFormatException ignored) {
            }
            if (length > Relay.MAX_BODY) {
                reply(out, method, 413, "text/html; charset=utf-8", page("Too large", "Max. 8 MB through the relay."), null);
                return;
            }
            byte[] body = readBody(in, (int) length);
            boolean cacheable = method.equals("GET") && path.startsWith("/static/");
            Object[] cached = cacheable ? staticCache.get(path) : null;
            if (cached != null) {
                reply(out, method, 200, (String) cached[0], (byte[]) cached[1], null);
                return;
            }
            long started = System.currentTimeMillis();
            Relay.Answer answer;
            try {
                slots.acquireUninterruptibly();
                try {
                    answer = relay.send(method, path, headers.getOrDefault("content-type", ""), body);
                } finally {
                    slots.release();
                }
            } catch (Relay.RelayException e) {
                log.line("! " + method + " " + path + ": " + e.getMessage());
                reply(out, method, 502, "text/html; charset=utf-8", page("Can't reach the bot", e.getMessage()), null);
                return;
            }
            log.line(method + " " + shorten(path) + " -> " + answer.status + " ("
                    + String.format(Locale.ROOT, "%.1f", (System.currentTimeMillis() - started) / 1000.0) + "s)");
            if (cacheable && answer.status == 200) staticCache.put(path, new Object[]{answer.contentType, answer.body});
            Map<String, String> extra = null;
            if (!answer.location.isEmpty()) {
                extra = new HashMap<>();
                extra.put("Location", answer.location);
            }
            reply(out, method, answer.status, answer.contentType, answer.body, extra);
        } catch (IOException | RuntimeException e) {
            log.line("! connection: " + e);
        }
    }

    private static String readHead(InputStream in) throws IOException {
        ByteArrayOutputStream buf = new ByteArrayOutputStream();
        int state = 0;
        while (true) {
            int b = in.read();
            if (b < 0) return buf.size() == 0 ? null : buf.toString("ISO-8859-1");
            buf.write(b);
            state = (b == '\r' && (state == 0 || state == 2)) ? state + 1
                    : (b == '\n' && (state == 1 || state == 3)) ? state + 1 : 0;
            if (state == 4) {
                String text = buf.toString("ISO-8859-1");
                return text.substring(0, text.length() - 4);
            }
            if (buf.size() > 64 * 1024) throw new IOException("header too large");
        }
    }

    private static byte[] readBody(InputStream in, int length) throws IOException {
        byte[] body = new byte[length];
        int read = 0;
        while (read < length) {
            int n = in.read(body, read, length - read);
            if (n < 0) throw new IOException("body cut off");
            read += n;
        }
        return body;
    }

    private static void reply(OutputStream out, String method, int status, String contentType, byte[] body,
                              Map<String, String> extra) throws IOException {
        StringBuilder head = new StringBuilder("HTTP/1.1 ").append(status).append(' ').append(reason(status)).append("\r\n");
        if (!contentType.isEmpty()) head.append("Content-Type: ").append(contentType).append("\r\n");
        head.append("Content-Length: ").append(body.length).append("\r\n");
        head.append("Cache-Control: no-store\r\nConnection: close\r\n");
        if (extra != null) {
            for (Map.Entry<String, String> e : extra.entrySet()) {
                head.append(e.getKey()).append(": ").append(e.getValue().replaceAll("[\r\n]", "")).append("\r\n");
            }
        }
        head.append("\r\n");
        out.write(head.toString().getBytes(StandardCharsets.ISO_8859_1));
        if (!method.equals("HEAD")) out.write(body);
        out.flush();
    }

    private static String reason(int status) {
        switch (status) {
            case 200: return "OK";
            case 204: return "No Content";
            case 302: return "Found";
            case 303: return "See Other";
            case 307: return "Temporary Redirect";
            case 400: return "Bad Request";
            case 401: return "Unauthorized";
            case 403: return "Forbidden";
            case 404: return "Not Found";
            case 413: return "Payload Too Large";
            case 502: return "Bad Gateway";
            default: return "Status";
        }
    }

    static byte[] page(String title, String text) {
        return Relay.utf8Bytes("<!DOCTYPE html><html><head><meta charset='utf-8'>"
                + "<meta name='viewport' content='width=device-width, initial-scale=1'><title>Bot Panel</title></head>"
                + "<body style='font-family:sans-serif;max-width:640px;margin:40px auto;padding:0 16px'><h2>"
                + escape(title) + "</h2><p>" + escape(text) + "</p></body></html>");
    }

    static String escape(String text) {
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;");
    }

    private static String shorten(String path) {
        return path.length() > 70 ? path.substring(0, 70) : path;
    }
}
