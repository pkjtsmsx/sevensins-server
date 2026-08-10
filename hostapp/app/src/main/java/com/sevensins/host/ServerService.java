package com.sevensins.host;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.os.Build;
import android.os.IBinder;
import android.util.Log;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

/**
 * Foreground service that owns the embedded Python servers.
 *
 * It must be a FOREGROUND service, not a background one: Android freezes background
 * processes while another app (the game) is in front, and a frozen server means the
 * game blocks on a socket that never answers. The ongoing notification is the price of
 * staying alive, and even it is not sufficient on its own -- the user also has to grant
 * the battery-optimisation exemption the activity asks for.
 */
public class ServerService extends Service {
    public static final String ACTION_START = "com.sevensins.host.START";
    public static final String ACTION_STOP = "com.sevensins.host.STOP";
    private static final String CHANNEL = "sevensins-host";
    private static final int NOTE_ID = 1;
    private static final String TAG = "SevenSinsHost";

    private static volatile boolean running = false;

    public static boolean isRunning() {
        return running;
    }

    /** Start the Python runtime once per process. Safe to call repeatedly. */
    static Python python(android.content.Context context) {
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(context.getApplicationContext()));
        }
        return Python.getInstance();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent == null ? ACTION_START : intent.getAction();
        if (ACTION_STOP.equals(action)) {
            stopServers();
            stopForeground(true);
            stopSelf();
            return START_NOT_STICKY;
        }
        startForeground(NOTE_ID, buildNotification("Starting…"));
        new Thread(() -> {
            String status;
            try {
                PyObject main = python(this).getModule("main");
                status = main.callAttr("start_server", getFilesDir().getAbsolutePath())
                        .toString();
                running = true;
            } catch (Throwable t) {
                Log.e(TAG, "server start failed", t);
                status = "failed: " + t.getMessage();
            }
            Log.i(TAG, status);
            NotificationManager nm = getSystemService(NotificationManager.class);
            nm.notify(NOTE_ID, buildNotification(running ? "Server running" : status));
        }, "server-start").start();
        // START_STICKY: if Android kills us under pressure, come back -- the game may
        // still be running and would otherwise hang on the next request.
        return START_STICKY;
    }

    private void stopServers() {
        try {
            python(this).getModule("main").callAttr("stop_server");
        } catch (Throwable t) {
            Log.e(TAG, "server stop failed", t);
        }
        running = false;
    }

    @Override
    public void onDestroy() {
        stopServers();
        super.onDestroy();
    }

    private Notification buildNotification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                && nm.getNotificationChannel(CHANNEL) == null) {
            nm.createNotificationChannel(new NotificationChannel(
                    CHANNEL, "Seven Sins Host", NotificationManager.IMPORTANCE_LOW));
        }
        PendingIntent open = PendingIntent.getActivity(
                this, 0, new Intent(this, MainActivity.class),
                PendingIntent.FLAG_IMMUTABLE);
        Notification.Builder b = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                ? new Notification.Builder(this, CHANNEL)
                : new Notification.Builder(this);
        return b.setContentTitle("Seven Sins Host")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentIntent(open)
                .setOngoing(true)
                .build();
    }
}
