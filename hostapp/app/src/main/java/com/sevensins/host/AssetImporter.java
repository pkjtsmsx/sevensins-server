package com.sevensins.host;

import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.provider.OpenableColumns;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.File;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Imports the game's asset-bundle set from a user-picked tar.
 *
 * The payload is ~2.6 GB, so this STREAMS the picker's InputStream straight into a
 * native `tar -x` process rather than copying the archive to storage first and
 * extracting afterwards. That avoids needing twice the space at peak on a device that
 * may not have it, and skips a full write+read of several gigabytes. Tar extraction is
 * sequential, so it never needs to seek — piping is safe.
 *
 * Native tar, not Java: unpacking tens of thousands of files through a JVM tar
 * implementation is far slower, the same reason reTBHost shells out to toybox.
 *
 * Expected archive layout — created from the server's patch_root, e.g.
 *     tar -cf bundles.tar -C server/patch_root bundles Android_AssetBundles
 * so entries are `bundles/<name>.ab`. It is extracted with -C patch_root. A flat archive
 * of bare .ab files also works if extracted into bundles/, because bundle_server falls
 * back to looking up the basename there.
 */
class AssetImporter {
    private static final String TAG = "SevenSinsHost";

    interface Progress {
        /** @param done bytes consumed, @param total bytes expected (0 = unknown) */
        void update(long done, long total);
    }

    /**
     * @return a short human-readable result string
     */
    static String importFrom(Context ctx, Uri uri, Progress progress) {
        File dest = new File(new File(ctx.getFilesDir(), "sevensins"), "patch_root");
        if (!dest.isDirectory() && !dest.mkdirs()) {
            return "cannot create " + dest;
        }
        long total = querySize(ctx, uri);
        Process proc = null;
        try (InputStream raw = ctx.getContentResolver().openInputStream(uri)) {
            if (raw == null) {
                return "could not open the selected file";
            }
            BufferedInputStream in = new BufferedInputStream(raw, 1 << 16);
            // Sniff the first two bytes for the gzip magic so a .tar.gz works too. The
            // mark/reset is why the stream is buffered.
            in.mark(2);
            int b0 = in.read(), b1 = in.read();
            in.reset();
            boolean gzip = (b0 == 0x1f && b1 == 0x8b);

            ProcessBuilder pb = new ProcessBuilder(
                    gzip ? new String[]{"tar", "-xz", "-C", dest.getAbsolutePath()}
                         : new String[]{"tar", "-x", "-C", dest.getAbsolutePath()});
            pb.redirectErrorStream(true);
            proc = pb.start();

            // Drain tar's output on a separate thread: a full pipe buffer would
            // deadlock us while we are still writing to its stdin.
            final Process p = proc;
            StringBuilder errTail = new StringBuilder();
            Thread drain = new Thread(() -> {
                try (java.io.BufferedReader r = new java.io.BufferedReader(
                        new java.io.InputStreamReader(p.getInputStream()))) {
                    String line;
                    while ((line = r.readLine()) != null) {
                        if (errTail.length() < 2000) errTail.append(line).append('\n');
                    }
                } catch (Exception ignored) {
                }
            }, "tar-drain");
            drain.setDaemon(true);
            drain.start();

            byte[] buf = new byte[1 << 20];
            long done = 0;
            try (OutputStream out = proc.getOutputStream()) {
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                    done += n;
                    if (progress != null) progress.update(done, total);
                }
            }
            int rc = proc.waitFor();
            drain.join(2000);
            if (rc != 0) {
                // toybox tar exits non-zero on benign per-file warnings (mtime and
                // permission complaints under app storage) even when every member was
                // written, so report it without discarding what landed.
                Log.w(TAG, "tar rc=" + rc + " output: " + errTail);
                return "extracted with warnings (rc=" + rc + ")";
            }
            return "import complete";
        } catch (Exception e) {
            Log.e(TAG, "asset import failed", e);
            if (proc != null) proc.destroy();
            return "failed: " + e.getMessage();
        }
    }

    private static long querySize(Context ctx, Uri uri) {
        try (Cursor c = ctx.getContentResolver().query(uri, null, null, null, null)) {
            if (c != null && c.moveToFirst()) {
                int i = c.getColumnIndex(OpenableColumns.SIZE);
                if (i >= 0 && !c.isNull(i)) return c.getLong(i);
            }
        } catch (Exception ignored) {
        }
        return 0;
    }
}
