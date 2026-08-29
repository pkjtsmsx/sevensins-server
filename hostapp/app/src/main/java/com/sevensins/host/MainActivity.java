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
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import com.chaquo.python.PyObject;

import org.json.JSONObject;

import java.util.LinkedHashMap;
import java.util.Map;

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

        Button editSave = new Button(this);
        editSave.setText("Edit save");
        editSave.setOnClickListener(v -> openSaveEditor());
        root.addView(editSave);

        // Server-WIDE, unlike "Edit save" right above it, which is per-account. That is
        // why rates live here on the main screen instead of inside the editor: they are
        // not a property of any one save.
        Button rates = new Button(this);
        rates.setText("Server rates");
        rates.setOnClickListener(v -> openRateSettings());
        root.addView(rates);

        Button crashLog = new Button(this);
        crashLog.setText("View crash log");
        crashLog.setOnClickListener(v -> openCrashLog());
        root.addView(crashLog);

        Button update = new Button(this);
        update.setText("Check for updates");
        update.setOnClickListener(v -> runUpdateCheck());
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

    /**
     * Server-wide reward rates.
     *
     * These are MULTIPLIERS against the reconstructed values, never absolute amounts:
     * 1.0 means "what the retail game paid", and the evidence tables in the server stay
     * untouched as the record of what that was. A rate change is therefore always a
     * visible, deliberate departure rather than something that can arrive disguised as
     * a bug fix.
     *
     * The dialog is built from whatever `settings.RATES` contains rather than a fixed
     * list of fields, so ADDING A RATE NEEDS NO APK REBUILD -- settings.py is
     * hot-updatable and this screen picks the new name up on its own. That is also why
     * the Java side only ever passes JSON around and knows none of the rules.
     *
     * Works with the server stopped: rates are a file beside the accounts.
     */
    private void openRateSettings() {
        String json;
        try {
            json = ServerService.python(this).getModule("main")
                                .callAttr("rates_json", getFilesDir().getAbsolutePath())
                                .toString();
        } catch (Throwable t) {
            Toast.makeText(this, "Could not read rates: " + t, Toast.LENGTH_LONG).show();
            return;
        }

        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        int pad = (int) (16 * getResources().getDisplayMetrics().density);
        box.setPadding(pad, pad, pad, pad);

        TextView blurb = new TextView(this);
        blurb.setText("Multipliers on the original game's values. 1.0 = retail. "
                    + "Takes effect immediately -- no restart.");
        box.addView(blurb);

        final Map<String, EditText> fields = new LinkedHashMap<>();
        try {
            JSONObject parsed = new JSONObject(json);
            if (parsed.has("error")) {
                Toast.makeText(this, "Rates error: " + parsed.getString("error"),
                               Toast.LENGTH_LONG).show();
                return;
            }
            JSONObject values = parsed.getJSONObject("rates");
            java.util.Iterator<String> names = values.keys();
            while (names.hasNext()) {
                String name = names.next();
                TextView label = new TextView(this);
                label.setText(name);
                box.addView(label);
                EditText field = new EditText(this);
                field.setInputType(android.text.InputType.TYPE_CLASS_NUMBER
                                 | android.text.InputType.TYPE_NUMBER_FLAG_DECIMAL);
                field.setText(String.valueOf(values.getDouble(name)));
                box.addView(field);
                fields.put(name, field);
            }
        } catch (Throwable t) {
            Toast.makeText(this, "Could not parse rates: " + t, Toast.LENGTH_LONG).show();
            return;
        }

        ScrollView scroll = new ScrollView(this);
        scroll.addView(box);

        new AlertDialog.Builder(this)
                .setTitle("Server rates")
                .setView(scroll)
                .setPositiveButton("Save", (d, w) -> {
                    JSONObject out = new JSONObject();
                    for (Map.Entry<String, EditText> e : fields.entrySet()) {
                        String raw = e.getValue().getText().toString().trim();
                        if (raw.isEmpty()) {
                            continue;           // left blank = leave that rate alone
                        }
                        try {
                            out.put(e.getKey(), Double.parseDouble(raw));
                        } catch (Throwable ignored) {
                            // A field that will not parse is skipped rather than
                            // failing the whole save -- settings.write_rates ignores
                            // anything it cannot read anyway.
                        }
                    }
                    applyRates("set_rates_json", out.toString());
                })
                // Recovery, and the reason to keep it one tap away: getting back to a
                // faithful build must never require remembering what the defaults were.
                .setNeutralButton("Reset to retail", (d, w) -> applyRates("reset_rates_json", null))
                .setNegativeButton("Cancel", null)
                .show();
    }

    /** Call one of the rate-writing entry points and report what the server ended up with. */
    private void applyRates(String function, String payload) {
        try {
            PyObject main = ServerService.python(this).getModule("main");
            String dir = getFilesDir().getAbsolutePath();
            String json = (payload == null ? main.callAttr(function, dir)
                                           : main.callAttr(function, dir, payload)).toString();
            JSONObject parsed = new JSONObject(json);
            if (parsed.has("error")) {
                Toast.makeText(this, "Rates error: " + parsed.getString("error"),
                               Toast.LENGTH_LONG).show();
                return;
            }
            JSONObject changed = parsed.getJSONObject("changed");
            Toast.makeText(this, changed.length() == 0
                                 ? "Rates: retail (all 1.0)"
                                 : "Rates changed: " + changed.toString(),
                           Toast.LENGTH_LONG).show();
        } catch (Throwable t) {
            Toast.makeText(this, "Could not save rates: " + t, Toast.LENGTH_LONG).show();
        }
    }

    /**
     * Show the crash log.
     *
     * Under Chaquopy a traceback goes to logcat and nowhere else, and a phone hosting a
     * session is not attached to adb -- so before this, any crash during actual play was
     * unreadable by the time anyone thought to look. main.record_exception mirrors every
     * traceback to <filesDir>/sevensins/crash.log, including the per-connection threads
     * that previously failed completely silently.
     *
     * Deliberately does NOT require the server to be running: the case this matters most
     * for is the server having died, when it is by definition not running.
     */
    private void openCrashLog() {
        String text;
        try {
            text = ServerService.python(this).getModule("main")
                                .callAttr("crash_log_text", getFilesDir().getAbsolutePath())
                                .toString();
        } catch (Throwable t) {
            text = "Could not read the crash log: " + t;
        }

        TextView body = new TextView(this);
        body.setText(text);
        body.setTextIsSelectable(true);         // so a traceback can be copied out
        body.setTypeface(android.graphics.Typeface.MONOSPACE);
        body.setTextSize(11);
        int pad = (int) (12 * getResources().getDisplayMetrics().density);
        body.setPadding(pad, pad, pad, pad);

        ScrollView scroll = new ScrollView(this);
        scroll.addView(body);
        // Horizontal scrolling too: a traceback's paths are long and wrapping them makes
        // the file much harder to read on a phone.
        android.widget.HorizontalScrollView wide = new android.widget.HorizontalScrollView(this);
        wide.addView(scroll);

        new AlertDialog.Builder(this)
                .setTitle("Crash log")
                .setView(wide)
                .setPositiveButton("Close", null)
                .setNeutralButton("Clear", (d, w) -> {
                    try {
                        ServerService.python(this).getModule("main").callAttr("clear_crash_log");
                        Toast.makeText(this, "Crash log cleared", Toast.LENGTH_SHORT).show();
                    } catch (Throwable t) {
                        Toast.makeText(this, "Could not clear: " + t, Toast.LENGTH_LONG).show();
                    }
                })
                .show();
    }

    /**
     * Open the on-device save editor in the phone's browser.
     *
     * The account lives in getFilesDir(), which on an unrooted phone no file manager,
     * USB cable or PC tool can reach -- so the editor runs here, next to the servers,
     * and the browser is just its window. The URL is loopback, so nothing outside this
     * phone can reach an endpoint that rewrites saves without authentication.
     *
     * The editor only exists while the server is running (main.start_server threads it
     * alongside the other two), so say that plainly rather than opening a browser onto
     * a connection-refused page, which reads as "the app is broken".
     */
    private void openSaveEditor() {
        if (!ServerService.isRunning()) {
            Toast.makeText(this, "Start the server first -- the editor runs inside it",
                           Toast.LENGTH_LONG).show();
            return;
        }
        String url;
        try {
            url = ServerService.python(this).getModule("main")
                               .callAttr("editor_url").toString();
        } catch (Throwable t) {
            url = "http://127.0.0.1:8099/";
        }
        try {
            startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
        } catch (Throwable t) {
            // A phone with no browser able to handle the intent: hand over the address
            // rather than failing silently.
            Toast.makeText(this, "Open " + url + " in your browser",
                           Toast.LENGTH_LONG).show();
        }
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

    private void runUpdateCheck() {
        // A build with no sevensins.updateRepo (a fresh clone of the public source repo)
        // has nowhere to check. Say so plainly instead of failing against an empty URL.
        if (!UpdateManager.channelConfigured()) {
            new AlertDialog.Builder(this)
                    .setTitle("No update channel")
                    .setMessage("This build was made without an update channel. Set "
                              + "sevensins.updateRepo in hostapp/local.properties and "
                              + "rebuild to enable hot updates.")
                    .setPositiveButton("OK", null)
                    .show();
            return;
        }
        Toast.makeText(this, "Checking for updates…", Toast.LENGTH_SHORT).show();
        new Thread(() -> {
            UpdateManager.Result r = UpdateManager.checkAndApply(this, UpdateManager.UPDATE_URL);
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
