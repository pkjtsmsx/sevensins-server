package com.sevensins.host;

import android.content.Context;
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
 * Pulls a code-only hot-update (tools/build_hostapp_update.py's output, published by
 * tools/publish_hostapp_update.py as a GitHub Release) over HTTPS and applies it WITHOUT
 * reinstalling the APK. See main.py's "HOT UPDATES" doc comment for how the next server
 * start picks the applied snapshot up (it shadows the shipped copy on sys.path; a
 * bad/missing snapshot falls back to the shipped copy automatically).
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

    /** Release-asset host for update.json + server_update.zip -- a small, DEDICATED
     * public repo (never the main project repo: this one carries only the compiled
     * server code the update zip contains, nothing from the reverse-engineering side).
     * It has to be PUBLIC: the app downloads both assets anonymously, with no token.
     * "latest" always resolves to whatever tools/publish_hostapp_update.py published
     * most recently, regardless of tag.
     *
     * The owner/name is configured per-machine and baked in at build time from
     * hostapp/local.properties (see app/build.gradle). Empty means "no channel
     * configured", which is a clean no-op, not a crash. */
    static final String UPDATE_REPO = BuildConfig.UPDATE_REPO;

    static final String UPDATE_URL = UPDATE_REPO.isEmpty() ? ""
            : "https://github.com/" + UPDATE_REPO + "/releases/latest/download/";

    /** False for a build made without sevensins.updateRepo -- e.g. from a fresh clone of
     * the public source repo, which deliberately does not name the update channel. */
    static boolean channelConfigured() {
        return !UPDATE_URL.isEmpty();
    }

    enum Outcome { UP_TO_DATE, APPLIED, FAILED }

    static class Result {
        final Outcome outcome;
        final String detail;

        Result(Outcome outcome, String detail) {
            this.outcome = outcome;
            this.detail = detail;
        }
    }

    private static File updateRoot(Context ctx) {
        return new File(new File(ctx.getFilesDir(), "sevensins"), "server_update");
    }

    /** The short-hash name of whatever snapshot is currently wired up, or null if there
     * isn't one (shipped code, or a corrupt/unreadable pointer -- either way "not
     * active" is the safe reading, never a crash). This is checkAndApply's ONLY source
     * of "am I up to date" -- not a separately-tracked preference, so it can never drift
     * from what main.py will actually load on the next start (in particular: resetting
     * clears this too, since resetToShipped deletes the very file this reads). */
    private static String readActiveHash(File root) {
        File active = new File(root, "active.txt");
        try (java.io.BufferedReader r = new java.io.BufferedReader(
                new java.io.FileReader(active))) {
            String line = r.readLine();
            return line == null ? null : line.trim();
        } catch (IOException e) {
            return null;
        }
    }

    /** Drops the active.txt pointer so the next server start falls back to the code the
     * APK shipped with -- the recovery path for a hot update that turns out to be bad.
     * Never touches accounts/design_cache/patch_root; those are siblings, not children,
     * of server_update/. The already-extracted snapshot directory is left in place (not
     * deleted), so re-applying the SAME update later is instant -- see checkAndApply. */
    static boolean resetToShipped(Context ctx) {
        File active = new File(updateRoot(ctx), "active.txt");
        return !active.exists() || active.delete();
    }

    static Result checkAndApply(Context ctx, String baseUrl) {
        String base = baseUrl.endsWith("/") ? baseUrl : baseUrl + "/";
        File root = updateRoot(ctx);
        File zipFile = new File(root, "_download.zip");
        try {
            JSONObject manifest = new JSONObject(httpGetString(base + "update.json"));
            String sha256 = manifest.getString("sha256");
            long size = manifest.optLong("size", 0);
            String file = manifest.optString("file", "server_update.zip");
            String shortHash = sha256.substring(0, 16);

            if (shortHash.equalsIgnoreCase(readActiveHash(root))) {
                return new Result(Outcome.UP_TO_DATE,
                        "already active: " + sha256.substring(0, 12));
            }
            if (!root.isDirectory() && !root.mkdirs()) {
                return new Result(Outcome.FAILED, "cannot create " + root);
            }

            // A snapshot for this exact hash may already sit on disk from a PRIOR apply
            // that was later reset (reset only clears the pointer, not the extraction) --
            // reactivate it instantly instead of re-downloading identical bytes.
            File extractDir = new File(root, shortHash);
            boolean reused = new File(extractDir, "titan_server.py").isFile();
            if (!reused) {
                downloadTo(base + file, zipFile, size);

                String actualSha = sha256Hex(zipFile);
                if (!actualSha.equalsIgnoreCase(sha256)) {
                    zipFile.delete();
                    return new Result(Outcome.FAILED,
                            "checksum mismatch (expected " + sha256.substring(0, 12)
                            + ", got " + actualSha.substring(0, 12)
                            + ") -- download corrupted?");
                }

                deleteRecursive(extractDir);      // any stale partial under this name
                extractZip(zipFile, extractDir, root);
                zipFile.delete();

                if (!new File(extractDir, "titan_server.py").isFile()) {
                    deleteRecursive(extractDir);
                    return new Result(Outcome.FAILED,
                            "extracted update is missing titan_server.py -- not activating");
                }
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

            pruneOldSnapshots(root, shortHash);
            String verb = reused ? "reactivated (already had this build)"
                                 : "downloaded and applied";
            return new Result(Outcome.APPLIED,
                    manifest.optInt("file_count", 0) + " files (" + size + " bytes), "
                    + sha256.substring(0, 12) + " -- " + verb
                    + "; restart the app to run it");
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
