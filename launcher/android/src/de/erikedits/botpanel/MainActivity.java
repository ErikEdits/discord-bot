package de.erikedits.botpanel;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.SharedPreferences;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.text.InputType;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.Menu;
import android.view.MenuItem;
import android.view.View;
import android.webkit.CookieManager;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ProgressBar;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.util.Map;

/**
 * Bot Panel app: paste the setup code from "/panel-launcher get system:Android" once, then the
 * app opens the bot's web panel. It works like the PC launcher - everything goes through Discord.
 */
public class MainActivity extends Activity {
    private static final String TAG = "BotPanel";
    private static final int MENU_RELOAD = 1;
    private static final int MENU_DISCONNECT = 2;

    private SharedPreferences prefs;
    private LocalServer server;
    private WebView web;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("panel", MODE_PRIVATE);
        String code = prefs.getString("code", null);
        if (code != null) {
            connect(code, true);
        } else {
            showSetup(null, "");
        }
    }

    @Override
    protected void onDestroy() {
        stopPanel();
        super.onDestroy();
    }

    @Override
    public void onBackPressed() {
        if (web != null && web.canGoBack()) {
            web.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    public boolean onCreateOptionsMenu(Menu menu) {
        menu.clear();
        if (web != null) {
            menu.add(Menu.NONE, MENU_RELOAD, Menu.NONE, "Reload");
            menu.add(Menu.NONE, MENU_DISCONNECT, Menu.NONE, "Remove setup code");
        }
        return true;
    }

    @Override
    public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == MENU_RELOAD && web != null) {
            web.reload();
            return true;
        }
        if (item.getItemId() == MENU_DISCONNECT) {
            new AlertDialog.Builder(this)
                    .setTitle("Remove setup code?")
                    .setMessage("The app forgets the setup code on this phone. You need a new code from "
                            + "/panel-launcher get to connect again.")
                    .setPositiveButton("Remove", (d, w) -> {
                        prefs.edit().remove("code").apply();
                        CookieManager.getInstance().removeAllCookies(null);
                        stopPanel();
                        showSetup(null, "");
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
            return true;
        }
        return super.onOptionsItemSelected(item);
    }

    // -------- Screens -------------------------------------------------------

    private void showSetup(String error, String prefill) {
        setTitle("Bot Panel");
        LinearLayout box = column();
        box.addView(text("Set up the Bot Panel", 22, true));
        box.addView(text("1. In Discord, run /panel-launcher get and choose Android.\n"
                + "2. The bot sends you a DM with a setup code (it starts with " + Setup.PREFIX + ").\n"
                + "3. Long-press that message, tap \"Copy Text\" and paste it here.", 15, false));
        EditText input = new EditText(this);
        input.setHint(Setup.PREFIX + "...");
        input.setMinLines(3);
        input.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE
                | InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS);
        input.setText(prefill);
        box.addView(input);
        Button paste = button("Paste");
        paste.setOnClickListener(v -> {
            ClipboardManager clipboard = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
            ClipData clip = clipboard == null ? null : clipboard.getPrimaryClip();
            if (clip != null && clip.getItemCount() > 0) {
                input.setText(clip.getItemAt(0).coerceToText(this));
            } else {
                Toast.makeText(this, "The clipboard is empty.", Toast.LENGTH_SHORT).show();
            }
        });
        box.addView(paste);
        Button go = button("Connect");
        go.setOnClickListener(v -> connect(input.getText().toString(), false));
        box.addView(go);
        if (error != null) {
            TextView err = text(error, 15, false);
            err.setTextColor(Color.rgb(237, 66, 69));
            box.addView(err);
        }
        TextView note = text("The setup code is your personal admin key for the panel - don't share it. "
                + "Lost your phone? /panel-launcher revoke device:Android makes it stop working.", 13, false);
        note.setAlpha(0.7f);
        box.addView(note);
        show(box);
    }

    private void showBusy(String message) {
        LinearLayout box = column();
        box.setGravity(Gravity.CENTER);
        box.addView(new ProgressBar(this));
        TextView t = text(message, 16, false);
        t.setGravity(Gravity.CENTER);
        box.addView(t);
        show(box);
    }

    /** The saved code didn't connect (no internet, bot offline): try again or use a new code. */
    private void showRetry(String error, String code) {
        LinearLayout box = column();
        box.addView(text("Can't open the panel", 22, true));
        box.addView(text(error, 15, false));
        Button retry = button("Try again");
        retry.setOnClickListener(v -> connect(code, true));
        box.addView(retry);
        Button other = button("Enter a new setup code");
        other.setOnClickListener(v -> showSetup(null, ""));
        box.addView(other);
        show(box);
    }

    private void showPanel(LocalServer local, String serverName) {
        server = local;
        setTitle(serverName.isEmpty() ? "Bot Panel" : serverName);
        CookieManager.getInstance().setAcceptCookie(true);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setBuiltInZoomControls(true);
        settings.setDisplayZoomControls(false);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if ("127.0.0.1".equals(uri.getHost())) return false;
                try {   // Discord / Modrinth links etc. open in their apps or the browser
                    startActivity(new Intent(Intent.ACTION_VIEW, uri));
                } catch (ActivityNotFoundException ignored) {
                }
                return true;
            }
        });
        setContentView(web);
        invalidateOptionsMenu();
        web.loadUrl(local.startUrl());
    }

    // -------- Connecting ----------------------------------------------------

    private static final class Result {
        String error;
        boolean badCode;
        String user = "";
        String serverName = "";
        LocalServer local;
    }

    private void connect(String code, boolean saved) {
        final Setup setup;
        try {
            setup = Setup.parse(code);
        } catch (IllegalArgumentException e) {
            showSetup(e.getMessage(), code);
            return;
        }
        stopPanel();
        showBusy("Connecting to the bot through Discord...");
        new Thread(() -> {
            Result r = new Result();
            Relay relay = setup.relay();
            try {
                if (!relay.webhookExists()) {
                    r.badCode = true;
                    throw new Relay.RelayException("This setup code no longer works (the relay was reset). "
                            + "Get a new one with /panel-launcher get.");
                }
                Relay.Answer ping = relay.send("GET", "/__relay/ping", "", new byte[0]);
                if (ping.status != 200) {
                    r.badCode = ping.status == 401 || ping.status == 403 || ping.status == 426;
                    throw new Relay.RelayException("The bot refused the app: " + reason(ping.text()));
                }
                Map<String, Object> info = Json.object(ping.text());
                r.user = Json.str(info, "user");
                r.serverName = Json.str(info, "server");
                r.local = new LocalServer(relay, line -> Log.i(TAG, line));
                r.local.start();
            } catch (Exception e) {
                r.error = e.getMessage() == null ? e.toString() : e.getMessage();
                if (r.local != null) r.local.stop();
            }
            runOnUiThread(() -> {
                if (isFinishing() || isDestroyed()) {
                    if (r.local != null) r.local.stop();
                    return;
                }
                if (r.error != null) {
                    if (r.badCode) prefs.edit().remove("code").apply();
                    if (saved && !r.badCode) showRetry(r.error, code);
                    else showSetup(r.error, saved ? "" : code);
                    return;
                }
                prefs.edit().putString("code", setup.code).apply();
                showPanel(r.local, r.serverName);
                Toast.makeText(this, "Connected as " + r.user, Toast.LENGTH_SHORT).show();
            });
        }, "panel-connect").start();
    }

    private static String reason(String html) {
        String text = html;
        int p = text.indexOf("<p>");
        if (p >= 0) {
            text = text.substring(p + 3);
            int end = text.indexOf("</p>");
            if (end >= 0) text = text.substring(0, end);
        }
        text = text.replaceAll("<[^>]+>", "");
        return text.length() > 300 ? text.substring(0, 300) : text;
    }

    private void stopPanel() {
        if (server != null) {
            server.stop();
            server = null;
        }
        if (web != null) {
            web.destroy();
            web = null;
        }
        invalidateOptionsMenu();
    }

    // -------- Layout helpers ------------------------------------------------

    private void show(View content) {
        ScrollView scroll = new ScrollView(this);
        scroll.setFillViewport(true);
        scroll.addView(content);
        setContentView(scroll);
        invalidateOptionsMenu();
    }

    private LinearLayout column() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setPadding(dp(20), dp(24), dp(20), dp(24));
        return box;
    }

    private TextView text(String value, int sp, boolean bold) {
        TextView t = new TextView(this);
        t.setText(value);
        t.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        if (bold) t.setTypeface(t.getTypeface(), android.graphics.Typeface.BOLD);
        t.setPadding(0, dp(6), 0, dp(10));
        return t;
    }

    private Button button(String label) {
        Button b = new Button(this);
        b.setText(label);
        b.setAllCaps(false);
        return b;
    }

    private int dp(int value) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value, getResources().getDisplayMetrics());
    }
}
