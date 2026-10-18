package de.erikedits.botpanel;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.IBinder;
import android.util.Log;

import java.util.HashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * Keeps a connection to the bot (through the panel relay) and shows its notifications:
 * new tickets, reported messages, ban appeals, errors ... (cogs/push.py, /api/push/wait).
 * Runs as a foreground service, so Android shows a small permanent "Bot Panel is running"
 * notification. Turn it off in the app menu ("Notifications: off").
 */
public class PushService extends Service {
    static final String CHANNEL_RUNNING = "running";
    static final String CHANNEL_ALERTS = "alerts";
    private static final String TAG = "BotPanelPush";
    private static final int RUNNING_ID = 1;

    private volatile boolean running;
    private Thread worker;
    private int nextId = 100;

    /** Notification kinds sent by the bot (cogs/push.py KINDS) and their names in the menu. */
    static final String[][] KINDS = {
            {"ticket", "New tickets"},
            {"urgent", "Urgent tickets"},
            {"report", "Reported messages"},
            {"appeal", "Ban appeals"},
            {"alt", "Possible alt accounts"},
            {"invite", "Invite requests"},
            {"flag", "Compromised accounts"},
            {"panel", "New panel devices"},
            {"error", "Bot errors"},
    };

    static boolean kindEnabled(Context context, String kind) {
        Set<String> off = context.getSharedPreferences("panel", MODE_PRIVATE).getStringSet("push_off", null);
        return off == null || !off.contains(kind);
    }

    static void saveKinds(Context context, boolean[] checked) {
        Set<String> off = new HashSet<>();
        for (int i = 0; i < KINDS.length && i < checked.length; i++) {
            if (!checked[i]) off.add(KINDS[i][0]);
        }
        context.getSharedPreferences("panel", MODE_PRIVATE).edit().putStringSet("push_off", off).apply();
    }

    static boolean enabled(Context context) {
        return context.getSharedPreferences("panel", MODE_PRIVATE).getBoolean("push", true);
    }

    static void start(Context context) {
        Intent intent = new Intent(context, PushService.class);
        context.startForegroundService(intent);
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, PushService.class));
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel quiet = new NotificationChannel(CHANNEL_RUNNING, "Bot Panel running",
                NotificationManager.IMPORTANCE_MIN);
        quiet.setShowBadge(false);
        nm.createNotificationChannel(quiet);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_ALERTS, "Bot alerts",
                NotificationManager.IMPORTANCE_HIGH));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        Notification note = new Notification.Builder(this, CHANNEL_RUNNING)
                .setSmallIcon(android.R.drawable.stat_notify_sync_noanim)
                .setContentTitle("Bot Panel is running")
                .setContentText("You get a notification for new tickets, reports and more.")
                .setContentIntent(openApp("/"))
                .setOngoing(true)
                .build();
        startForeground(RUNNING_ID, note);
        if (!running) {
            running = true;
            worker = new Thread(this::loop, "panel-push");
            worker.setDaemon(true);
            worker.start();
        }
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        running = false;
        if (worker != null) worker.interrupt();
        super.onDestroy();
    }

    private PendingIntent openApp(String path) {
        Intent open = new Intent(this, MainActivity.class);
        open.putExtra("path", path);
        open.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        return PendingIntent.getActivity(this, path.hashCode(), open,
                PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE);
    }

    private void loop() {
        SharedPreferences prefs = getSharedPreferences("panel", MODE_PRIVATE);
        long since = prefs.getLong("push_seq", -1);
        String boot = prefs.getString("push_boot", "");
        long backoff = 5_000;
        while (running) {
            String code = prefs.getString("code", null);
            if (code == null) {
                stopSelf();
                return;
            }
            try {
                Relay relay = Setup.parse(code).relay();
                Relay.Answer a = relay.send("GET", "/api/push/wait?since=" + Math.max(since, 0) + "&timeout=20", "",
                        new byte[0]);
                if (a.status == 401 || a.status == 403) {
                    Log.i(TAG, "Not allowed anymore - stopping");
                    stopSelf();
                    return;
                }
                if (a.status != 200) throw new Relay.RelayException("HTTP " + a.status);
                Map<String, Object> data = Json.object(a.text());
                long seq = (long) Double.parseDouble(Json.str(data, "seq"));
                String newBoot = Json.str(data, "boot");
                boolean first = since < 0 || !newBoot.equals(boot);   // first start / bot restarted: don't replay
                Object items = data.get("items");
                if (!first && items instanceof List) {
                    for (Object o : (List<?>) items) {
                        if (o instanceof Map) {
                            @SuppressWarnings("unchecked")
                            Map<String, Object> item = (Map<String, Object>) o;
                            if (!kindEnabled(this, Json.str(item, "kind"))) continue;
                            show(Json.str(item, "title"), Json.str(item, "body"), Json.str(item, "path"));
                        }
                    }
                }
                since = seq;
                boot = newBoot;
                prefs.edit().putLong("push_seq", since).putString("push_boot", boot).apply();
                backoff = 5_000;
            } catch (Exception e) {
                Log.i(TAG, "Push check failed: " + e.getMessage());
                sleep(backoff);
                backoff = Math.min(backoff * 2, 120_000);
            }
        }
    }

    private void show(String title, String body, String path) {
        Notification note = new Notification.Builder(this, CHANNEL_ALERTS)
                .setSmallIcon(android.R.drawable.stat_notify_chat)
                .setContentTitle(title.isEmpty() ? "Bot Panel" : title)
                .setContentText(body)
                .setStyle(new Notification.BigTextStyle().bigText(body))
                .setContentIntent(openApp(path.isEmpty() ? "/" : path))
                .setAutoCancel(true)
                .build();
        getSystemService(NotificationManager.class).notify(nextId++, note);
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
        }
    }
}
