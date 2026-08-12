package com.sevensins.host;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import org.json.JSONObject;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * Pulls a code-only hot-update (tools/build_hostapp_update.py's output, served by
 * tools/serve_hostapp_update.py) over plain HTTP and applies it WITHOUT reinstalling the
 * APK. See main.py's "HOT UPDATES" doc comment for how the next server start picks the
 * applied snapshot up (it shadows the shipped copy on sys.path; a bad/missing snapshot
 * falls back to the shipped copy automatically).
 *
 * ACCOUNT SAFETY: every path this class writes lives under its own
 * <filesDir>/sevensins/server_update/ directory, never inside accounts/, design_cache/ or
 * patch_root/ -- and the zip itself is code + battle_data only, enforced on the BUILD side
 * by build_hostapp_update.py's file list. A hot update cannot touch a save, and neither can
 * the "reset to shipped code" recovery path (it only ever removes the active.txt pointer).
 *
 * Runs entirely on the calling thread; callers must not invoke this from the UI thread.
 */
class UpdateManager {
    private static final String TAG = "SevenSinsHost";
    private static final String PREFS = "host";
    private static final String KEY_SHA = "update_sha256";
    private static final String KEY_URL = "update_base_url";

    enum Outcome { UP_TO_DATE, APPLIED, FAILED }

    static class Result {
        final Outcome outcome;
        final String detail;

        Result(Outcome outcome, String detail) {
            this.outcome = outcome;
            this.detail = detail;
        }
    }

    static String savedBaseUrl(Context ctx) {
        return ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_URL, "");
    }

    static void saveBaseUrl(Context ctx, String url) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
           .edit().putString(KEY_URL, url).apply();
    }

    private static File updateRoot(Context ctx) {
        return new File(new File(ctx.getFilesDir(), "sevensins"), "server_update");
    }

    /** Drops the active.txt pointer so the next server start falls back to the code the
     * APK shipped with -- the recovery path for a hot update that turns out to be bad.
     * Never touches accounts/design_cache/patch_root; those are siblings, not children,
     * of server_update/. */
    static boolean resetToShipped(Context ctx) {
        File active = new File(updateRoot(ctx), "active.txt");
        return !active.exists() || active.delete();
    }

    static Result checkAndApply(Context ctx, String baseUrl) {
        String base = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
        SharedPreferences prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE);
        File root = updateRoot(ctx);
        File zipFile = new File(root, "_download.zip");
        try {
            JSONObject manifest = new JSONObject(httpGetString(base + "update.json"));
            String sha256 = manifest.getString("sha256");
            long size = manifest.optLong("size", 0);
            String file = manifest.optString("file", "server_update.zip");

            if (sha256.equalsIgnoreCase(prefs.getString(KEY_SHA, ""))) {
                return new Result(Outcome.UP_TO_DATE,
                        "already on " + sha256.substring(0, 12));
            }
            if (!root.isDirectory() && !root.mkdirs()) {
                return new Result(Outcome.FAILED, "cannot create " + root);
            }

            downloadTo(base + file, zipFile, size);

            String actualSha = sha256Hex(zipFile);
            if (!actualSha.equalsIgnoreCase(sha256)) {
                zipFile.delete();
                return new Result(Outcome.FAILED,
                        "checksum mismatch (expected " + sha256.substring(0, 12)
                        + ", got " + actualSha.substring(0, 12) + ") -- download corrupted?");
            }

            // Directory name = the content hash, so re-applying the same update is a
            // no-op past the KEY_SHA check above, and a fresh sha256 can never collide
            // with a leftover extraction from a different build.
            String shortHash = sha256.substring(0, 16);
            File extractDir = new File(root, shortHash);
            deleteRecursive(extractDir);          // any stale partial under this name
            extractZip(zipFile, extractDir, root);
            zipFile.delete();

            if (!new File(extractDir, "titan_server.py").isFile()) {
                deleteRecursive(extractDir);
                return new Result(Outcome.FAILED,
                        "extracted update is missing titan_server.py -- not activating");
            }

            // Atomic write-then-rename: main.py reads active.txt at server-start time and
            // must never see a half-written pointer.
            File active = new File(root, "active.txt");
            File activeTmp = new File(root, "active.txt.tmp");
            try (FileOutputStream out = new FileOutputStream(activeTmp)) {
                out.write(shortHash.getBytes(StandardCharsets.UTF_8));
            }
            if (!activeTmp.renameTo(active)) {
                return new Result(Outcome.FAILED, "could not activate the new snapshot");
            }

            prefs.edit().putString(KEY_SHA, sha256).apply();
            saveBaseUrl(ctx, baseUrl);
            pruneOldSnapshots(root, shortHash);
            return new Result(Outcome.APPLIED,
                    manifest.optInt("file_count", 0) + " files (" + size + " bytes), "
                    + sha256.substring(0, 12) + " -- restart the app to run it");
        } catch (Exception e) {
            Log.e(TAG, "update check/apply failed", e);
            zipFile.delete();
            return new Result(Outcome.FAILED, String.valueOf(e.getMessage()));
        }
    }

    // ---- HTTP -------------------------------------------------------------------
    private static String httpGetString(String url) throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10_000);
        c.setReadTimeout(10_000);
        try (InputStream in = c.getInputStream()) {
            java.io.ByteArrayOutputStream buf = new java.io.ByteArrayOutputStream();
            byte[] chunk = new byte[8192];
            int n;
            while ((n = in.read(chunk)) > 0) buf.write(chunk, 0, n);
            return buf.toString("UTF-8");
        } finally {
            c.disconnect();
        }
    }

    private static void downloadTo(String url, File dest, long expectedSize)
            throws IOException {
        HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
        c.setConnectTimeout(10_000);
        c.setReadTimeout(30_000);
        try (InputStream in = c.getInputStream();
             OutputStream out = new FileOutputStream(dest)) {
            byte[] buf = new byte[1 << 16];
            int n;
            while ((n = in.read(buf)) > 0) out.write(buf, 0, n);
        } finally {
            c.disconnect();
        }
        if (expectedSize > 0 && dest.length() != expectedSize) {
            throw new IOException("downloaded " + dest.length() + " bytes, expected "
                                  + expectedSize);
        }
    }

    // ---- zip / hashing / cleanup --------------------------------------------------
    /** Extracts into `dest`, refusing any entry whose resolved path escapes `root` --
     * the archive is our own build tool's output, but this is cheap insurance against a
     * corrupted or crafted zip writing outside server_update/. */
    private static void extractZip(File zipFile, File dest, File root) throws IOException {
        String rootCanon = root.getCanonicalPath() + File.separator;
        try (ZipInputStream zin = new ZipInputStream(
                new java.io.BufferedInputStream(new java.io.FileInputStream(zipFile)))) {
            ZipEntry entry;
            while ((entry = zin.getNextEntry()) != null) {
                File out = new File(dest, entry.getName());
                if (!out.getCanonicalPath().startsWith(rootCanon)) {
                    throw new IOException("zip entry escapes server_update/: "
                                          + entry.getName());
                }
                if (entry.isDirectory()) {
                    out.mkdirs();
                    continue;
                }
                File parent = out.getParentFile();
                if (parent != null) parent.mkdirs();
                try (OutputStream os = new FileOutputStream(out)) {
                    byte[] buf = new byte[1 << 16];
                    int n;
                    while ((n = zin.read(buf)) > 0) os.write(buf, 0, n);
                }
            }
        }
    }

    private static String sha256Hex(File f) throws IOException {
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            try (InputStream in = new java.io.FileInputStream(f)) {
                byte[] buf = new byte[1 << 16];
                int n;
                while ((n = in.read(buf)) > 0) md.update(buf, 0, n);
            }
            StringBuilder sb = new StringBuilder();
            for (byte b : md.digest()) sb.append(String.format("%02x", b));
            return sb.toString();
        } catch (java.security.NoSuchAlgorithmException e) {
            throw new IOException(e);          // SHA-256 is always available on Android
        }
    }

    private static void deleteRecursive(File f) {
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) deleteRecursive(k);
        }
        f.delete();
    }

    /** Keeps only the just-activated snapshot dir (plus active.txt); an old snapshot is
     * pure disk cost once nothing points at it -- see resetToShipped() for the actual
     * rollback path (falls back to the APK's shipped copy, not to an old hot-update). */
    private static void pruneOldSnapshots(File root, String keepHash) {
        File[] kids = root.listFiles();
        if (kids == null) return;
        for (File k : kids) {
            String name = k.getName();
            if (k.isDirectory() && !name.equals(keepHash)) {
                deleteRecursive(k);
            }
        }
    }
}
