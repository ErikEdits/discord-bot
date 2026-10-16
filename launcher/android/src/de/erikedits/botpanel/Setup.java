package de.erikedits.botpanel;

import java.util.Base64;
import java.util.Map;

/**
 * The setup code from "/panel-launcher get system:Android":
 * "BOTPANEL1:" + base64url(JSON {"w": webhook URL, "k": key id, "s": secret (base64), "n": server name}).
 */
final class Setup {
    static final String PREFIX = "BOTPANEL1:";

    final String webhook;
    final String keyId;
    final byte[] secret;
    final String serverName;
    final String code;

    private Setup(String webhook, String keyId, byte[] secret, String serverName, String code) {
        this.webhook = webhook;
        this.keyId = keyId;
        this.secret = secret;
        this.serverName = serverName;
        this.code = code;
    }

    /** Accepts the code with spaces, line breaks or Discord's `code` backticks around it. */
    static Setup parse(String text) {
        String code = text == null ? "" : text.replaceAll("[\\s`]", "");
        int at = code.indexOf(PREFIX);
        if (at < 0) throw new IllegalArgumentException("That's not a setup code. It starts with " + PREFIX);
        code = code.substring(at);
        try {
            String json = Relay.utf8(Base64.getUrlDecoder().decode(code.substring(PREFIX.length())));
            Map<String, Object> m = Json.object(json);
            String webhook = Json.str(m, "w");
            if (!webhook.startsWith("https://") && !webhook.startsWith("http://127.0.0.1")) {
                throw new IllegalArgumentException("bad webhook");
            }
            byte[] secret = Relay.base64(Json.str(m, "s"));
            if (Json.str(m, "k").isEmpty() || secret.length < 16) throw new IllegalArgumentException("bad key");
            return new Setup(webhook, Json.str(m, "k"), secret, Json.str(m, "n"), code);
        } catch (RuntimeException e) {
            throw new IllegalArgumentException("The setup code is incomplete or damaged - copy the whole code again.");
        }
    }

    Relay relay() {
        return new Relay(webhook, keyId, secret);
    }
}
