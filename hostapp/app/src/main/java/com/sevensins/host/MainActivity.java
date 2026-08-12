package com.sevensins.host;

import android.Manifest;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.PowerManager;
import android.os.Process;
import android.provider.Settings;
import android.view.Gravity;
import android.view.View;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;

/**
 * One screen: start/stop the server, grant the battery exemption, import assets, launch
 * the game, and check for a code hot-update (see UpdateManager).
 *
 * Deliberately built in code rather than XML -- the whole UI is a handful of widgets,
 * and this keeps the app to a handful of files.
 */
public class MainActivity extends android.app.Activity {
    private static final String GAME = "com.userjoy.sineng";

    private TextView status;
    private Button toggle;

    @Override
    protected void onCreate(Bundle saved) {
        super.onCreate(saved);

        LinearLayout root = new LinearLayout(this);
        root.setOrientation(LinearLayout.VERTICAL);
        root.setGravity(Gravity.CENTER_HORIZONTAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        root.setPadding(pad, pad, pad, pad);

        TextView title = new TextView(this);
        title.setText("Seven Sins Host");
        title.setTextSize(22);
        title.setPadding(0, pad, 0, pad);
        root.addView(title);

        status = new TextView(this);
        status.setPadding(0, 0, 0, pad);
        root.addView(status);

        toggle = new Button(this);
        toggle.setOnClickListener(v -> onToggle());
        root.addView(toggle);

        Button battery = new Button(this);
        battery.setText("Allow background (battery)");
        battery.setOnClickListener(v -> requestBatteryExemption());
        root.addView(battery);

        Button importBtn = new Button(this);
        importBtn.setText("Import assets (tar)");
        importBtn.setOnClickListener(v -> pickArchive());
        root.addView(importBtn);

        Button launch = new Button(this);
        launch.setText("Launch Seven Sins");
        launch.setOnClickListener(v -> launchGame());
        root.addView(launch);

        Button update = new Button(this);
        update.setText("Check for updates");
        update.setOnClickListener(v -> promptAndCheckUpdate());
        // Long-press = recovery, not a second everyday button: fall back to the code the
        // APK shipped with, for the rare case a hot update turns out to be bad. It never
        // touches the account/asset data either, same as the update path itself.
        update.setOnLongClickListener(v -> {
            new AlertDialog.Builder(this)
                    .setTitle("Reset to shipped code?")
                    .setMessage("Drops the applied update and goes back to the code this "
                              + "APK was built with. Your account and imported assets are "
                              + "not touched. Restart the app afterwards.")
                    .setPositiveButton("Reset", (d, w) -> {
                        boolean ok = UpdateManager.resetToShipped(this);
                        Toast.makeText(this, ok ? "Reset -- restart the app"
                                                : "Could not reset", Toast.LENGTH_LONG).show();
                    })
                    .setNegativeButton("Cancel", null)
                    .show();
            return true;
        });
        root.addView(update);

        setContentView(root);

        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
            // The foreground service needs a visible notification to survive.
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
        }
        // Ask for the battery exemption ONCE, unprompted, on first launch. Waiting for
        // the user to discover the button means the common first experience is a game
        // that hangs the moment it comes to the foreground and Android freezes us --
        // and that failure looks like a broken server, not a power setting.
        android.content.SharedPreferences prefs =
                getSharedPreferences("host", MODE_PRIVATE);
        if (!prefs.getBoolean("asked_battery", false) && !isExempt()) {
            prefs.edit().putBoolean("asked_battery", true).apply();
            requestBatteryExemption();
        }
        refresh();
    }

    @Override
    protected void onResume() {
        super.onResume();
        refresh();
    }

    private void onToggle() {
        Intent i = new Intent(this, ServerService.class);
        if (ServerService.isRunning()) {
            i.setAction(ServerService.ACTION_STOP);
            startService(i);
        } else {
            i.setAction(ServerService.ACTION_START);
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(i);
            } else {
                startService(i);
            }
        }
        // The service does its work on a background thread; give it a moment before the
        // label is re-read, and refresh again on resume.
        toggle.postDelayed(this::refresh, 1200);
    }

    private void refresh() {
        boolean up = ServerService.isRunning();
        toggle.setText(up ? "Stop server" : "Start server");
        String detail;
        try {
            PyObject main = ServerService.python(this).getModule("main");
            detail = main.callAttr("status", getFilesDir().getAbsolutePath()).toString();
        } catch (Throwable t) {
            detail = "python not ready";
        }
        // The exemption is the difference between "works" and "hangs as soon as you
        // switch to the game", so its state is always on screen, not buried in a button.
        String power = isExempt()
                ? "Battery exemption granted."
                : "Battery exemption NOT granted — Android will freeze the server while "
                  + "the game is open, and the game will hang.";
        status.setText((up ? "Running — game :22110, bundles :8088" : "Stopped")
                + "\n" + detail + "\n\n" + power
                + "\n\nOn some vendors (realme/OPPO/Xiaomi) the exemption alone is not "
                + "enough: also allow background activity and auto-launch for this app, "
                + "or the vendor's own freezer suspends the server mid-game.");
    }

    private boolean isExempt() {
        PowerManager pm = getSystemService(PowerManager.class);
        return pm != null && pm.isIgnoringBatteryOptimizations(getPackageName());
    }

    @SuppressWarnings("BatteryLife")
    private void requestBatteryExemption() {
        if (isExempt()) {
            Toast.makeText(this, "Already granted", Toast.LENGTH_SHORT).show();
            return;
        }
        startActivity(new Intent(
                Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS,
                Uri.parse("package:" + getPackageName())));
    }

    private static final int REQ_PICK_ARCHIVE = 2;

    private void pickArchive() {
        if (ServerService.isRunning()) {
            Toast.makeText(this, "Stop the server before importing", Toast.LENGTH_LONG)
                 .show();
            return;
        }
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        // Tars have no reliable MIME type across providers, so accept anything and
        // detect the format by sniffing the stream instead.
        i.setType("*/*");
        startActivityForResult(Intent.createChooser(i, "Select the asset tar"),
                               REQ_PICK_ARCHIVE);
    }

    @Override
    protected void onActivityResult(int req, int result, Intent data) {
        super.onActivityResult(req, result, data);
        if (req != REQ_PICK_ARCHIVE || result != RESULT_OK || data == null
                || data.getData() == null) {
            return;
        }
        final android.net.Uri uri = data.getData();
        status.setText("Importing… 0%");
        new Thread(() -> {
            String out = AssetImporter.importFrom(this, uri, (done, total) -> {
                // The import moves gigabytes; without feedback it looks like a hang.
                String msg = total > 0
                        ? String.format(java.util.Locale.US, "Importing… %d%% (%.1f/%.1f GB)",
                                        done * 100 / total, done / 1e9, total / 1e9)
                        : String.format(java.util.Locale.US, "Importing… %.1f GB", done / 1e9);
                runOnUiThread(() -> status.setText(msg));
            });
            runOnUiThread(() -> {
                Toast.makeText(this, out, Toast.LENGTH_LONG).show();
                refresh();
            });
        }, "asset-import").start();
    }

    /** Asks for the dev box's update-server URL (remembered after the first time, since
     * it's normally the same LAN address every session -- see
     * tools/serve_hostapp_update.py), then runs the check off the UI thread. */
    private void promptAndCheckUpdate() {
        String saved = UpdateManager.savedBaseUrl(this);
        EditText input = new EditText(this);
        input.setHint("http://192.168.1.x:8089/");
        if (!saved.isEmpty()) input.setText(saved);
        new AlertDialog.Builder(this)
                .setTitle("Update server URL")
                .setMessage("Where tools/serve_hostapp_update.py is running on your "
                          + "dev machine.")
                .setView(input)
                .setPositiveButton("Check", (d, w) -> {
                    String url = input.getText().toString().trim();
                    if (url.isEmpty()) return;
                    runUpdateCheck(url);
                })
                .setNegativeButton("Cancel", null)
                .show();
    }

    private void runUpdateCheck(String url) {
        Toast.makeText(this, "Checking for updates…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            UpdateManager.Result r = UpdateManager.checkAndApply(this, url);
            runOnUiThread(() -> {
                switch (r.outcome) {
                    case UP_TO_DATE:
                        Toast.makeText(this, "Already up to date (" + r.detail + ")",
                                       Toast.LENGTH_LONG).show();
                        break;
                    case APPLIED:
                        new AlertDialog.Builder(this)
                                .setTitle("Update applied")
                                .setMessage(r.detail + "\n\nAccount and imported assets are "
                                          + "untouched. Restart the app now to run the new "
                                          + "code?")
                                .setPositiveButton("Restart now", (d, w) -> restartApp())
                                .setNegativeButton("Later", null)
                                .show();
                        break;
                    case FAILED:
                    default:
                        Toast.makeText(this, "Update failed: " + r.detail,
                                       Toast.LENGTH_LONG).show();
                }
            });
        }, "update-check").start();
    }

    /** Stops the server, then kills this process -- Chaquopy's interpreter only re-reads
     * modules from disk on a fresh process, so a partial in-process module reload is not
     * an option here (see main.py's HOT UPDATES note). The user reopens the app from the
     * launcher; nothing auto-relaunches it, which keeps this to one well-understood step
     * (kill) instead of AlarmManager scheduling that behaves differently across OEMs. */
    private void restartApp() {
        Intent stop = new Intent(this, ServerService.class);
        stop.setAction(ServerService.ACTION_STOP);
        startService(stop);
        Toast.makeText(this, "Closing — reopen Seven Sins Host to run the update",
                       Toast.LENGTH_LONG).show();
        toggle.postDelayed(() -> {
            finishAffinity();
            Process.killProcess(Process.myPid());
        }, 800);
    }

    private void launchGame() {
        final Intent i = getPackageManager().getLaunchIntentForPackage(GAME);
        if (i == null) {
            Toast.makeText(this, "Seven Sins is not installed", Toast.LENGTH_LONG).show();
            return;
        }
        if (ServerService.isRunning()) {
            startActivity(i);
            return;
        }
        // Launching the game with the server down is the single easiest mistake to make,
        // and its symptom is indistinguishable from a broken setup: the client sits on
        // "Retrieving patch manifest ... Retry n/10" and gives up. So start the server
        // first and wait for it to actually answer, rather than trusting the user to
        // remember the other button.
        Toast.makeText(this, "Starting the server first…", Toast.LENGTH_SHORT).show();
        Intent svc = new Intent(this, ServerService.class);
        svc.setAction(ServerService.ACTION_START);
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(svc);
        } else {
            startService(svc);
        }
        new Thread(() -> {
            // The service boots Python on a background thread; poll rather than guess.
            for (int n = 0; n < 40 && !ServerService.isRunning(); n++) {
                try {
                    Thread.sleep(250);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }
            runOnUiThread(() -> {
                refresh();
                if (ServerService.isRunning()) {
                    startActivity(i);
                } else {
                    Toast.makeText(this, "Server did not start — see the app's status",
                                   Toast.LENGTH_LONG).show();
                }
            });
        }, "launch-wait").start();
    }
}
