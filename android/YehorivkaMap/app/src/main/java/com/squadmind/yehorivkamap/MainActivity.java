package com.squadmind.yehorivkamap;

import android.app.Activity;
import android.content.ContentValues;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Log;
import android.view.View;
import android.view.Window;
import android.view.WindowManager;
import android.webkit.ConsoleMessage;
import android.webkit.JavascriptInterface;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.MessageDigest;

public class MainActivity extends Activity {
    private static final String URL = "file:///android_asset/maps/yehorivka.html";
    private static final int REQ_IMPORT = 4711;
    private static final int MAX_ARCHIVE_BYTES = 8 * 1024 * 1024;
    private static final long MAX_PACK_BYTES = 24L * 1024 * 1024;   // 地图数据包上限（实际约 3 MB）
    private static final int MAX_TEXT_BYTES = 2 * 1024 * 1024;      // manifest 之类的小文本上限
    private WebView webView;
    private TextView loading;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        applySystemBars();

        webView = findViewById(R.id.webView);
        loading = findViewById(R.id.loading);
        webView.setBackgroundColor(Color.parseColor("#0B0F14"));
        webView.setVerticalScrollBarEnabled(false);
        webView.setHorizontalScrollBarEnabled(false);
        webView.setOverScrollMode(View.OVER_SCROLL_NEVER);
        webView.setLongClickable(false);
        webView.setOnLongClickListener(v -> true);

        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(true);
        settings.setSupportZoom(false);
        settings.setBuiltInZoomControls(false);
        settings.setDisplayZoomControls(false);
        settings.setUseWideViewPort(true);
        settings.setLoadWithOverviewMode(false);
        settings.setTextZoom(100);
        settings.setMediaPlaybackRequiresUserGesture(true);

        webView.addJavascriptInterface(new ArchiveBridge(), "YehorivkaBridge");

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onConsoleMessage(ConsoleMessage message) {
                Log.d("YehorivkaMap", message.message() + " @" + message.lineNumber());
                return true;
            }
        });
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public void onPageFinished(WebView view, String url) {
                loading.setVisibility(View.GONE);
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) {
                    loading.setText("加载失败：" + error.getDescription());
                }
            }

        });
        webView.loadUrl(URL);
    }

    // ---------------------------------------------------------------- archive bridge

    private class ArchiveBridge {
        @JavascriptInterface
        public void exportArchive(final String fileName, final String json) {
            runOnUiThread(() -> doExport(fileName, json));
        }

        @JavascriptInterface
        public void pickArchive() {
            runOnUiThread(MainActivity.this::doPickArchive);
        }

        @JavascriptInterface
        public void toast(final String msg) {
            runOnUiThread(() -> Toast.makeText(MainActivity.this, msg, Toast.LENGTH_SHORT).show());
        }

        // ---- 0.39.0：地图数据包（GitHub 托管，页面里手动下载更新） ----

        @JavascriptInterface
        public String mapPackGet(String slug) {
            try {
                File f = packFile(slug);
                if (!f.isFile()) return "";
                return new String(readAll(f), "UTF-8");
            } catch (Exception e) {
                Log.w("YehorivkaMap", "mapPackGet failed", e);
                return "";
            }
        }

        @JavascriptInterface
        public String mapPackInfo(String slug) {
            try {
                JSONObject ent = readPackIndex().optJSONObject(slugOf(slug));
                File f = packFile(slug);
                if (ent == null || !f.isFile()) return "";
                JSONObject o = new JSONObject();
                o.put("v", ent.optString("v"));
                o.put("size", f.length());
                o.put("name", ent.optString("name"));
                return o.toString();
            } catch (Exception e) {
                return "";
            }
        }

        @JavascriptInterface
        public String mapPackList() {
            return readPackIndex().toString();
        }

        @JavascriptInterface
        public String mapPackDelete(String slug) {
            try {
                File f = packFile(slug);
                if (f.isFile() && !f.delete()) return "err:删不掉 " + f.getName();
                JSONObject idx = readPackIndex();
                idx.remove(slugOf(slug));
                writePackIndex(idx);
                return "ok";
            } catch (Exception e) {
                return "err:" + (e.getMessage() == null ? e.toString() : e.getMessage());
            }
        }

        @JavascriptInterface
        public void mapPackDownload(String slug, String url, String sha, String size, String ver, String reqId) {
            downloadPack(slug, url, sha, size, ver, reqId);
        }

        @JavascriptInterface
        public void netGet(String url, String reqId) {
            fetchText(url, reqId);
        }
    }

    /** 数据包目录：应用私有 files/maps；index.json 记每张图的版本与大小，供页面显示。 */
    private File packDir() {
        File d = new File(getFilesDir(), "maps");
        if (!d.isDirectory() && !d.mkdirs()) {
            Log.w("YehorivkaMap", "cannot create " + d);
        }
        return d;
    }

    private static String slugOf(String s) {
        String t = (s == null) ? "" : s.toLowerCase();
        StringBuilder b = new StringBuilder();
        for (int i = 0; i < t.length() && b.length() < 40; i++) {
            char c = t.charAt(i);
            if ((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_' || c == '-') b.append(c);
        }
        return b.length() == 0 ? "map" : b.toString();
    }

    private File packFile(String slug) {
        return new File(packDir(), slugOf(slug) + ".json");
    }

    private File packIndexFile() {
        return new File(packDir(), "index.json");
    }

    private static byte[] readAll(File f) throws Exception {
        ByteArrayOutputStream bos = new ByteArrayOutputStream();
        try (InputStream in = new java.io.FileInputStream(f)) {
            byte[] buf = new byte[65536];
            int n;
            while ((n = in.read(buf)) > 0) bos.write(buf, 0, n);
        }
        return bos.toByteArray();
    }

    private static void writeAtomic(File dst, byte[] data) throws Exception {
        File tmp = new File(dst.getParentFile(), dst.getName() + ".tmp");
        try (FileOutputStream out = new FileOutputStream(tmp)) {
            out.write(data);
            out.getFD().sync();
        }
        if (dst.exists() && !dst.delete()) throw new Exception("覆盖不了 " + dst.getName());
        if (!tmp.renameTo(dst)) throw new Exception("写入失败 " + dst.getName());
    }

    private synchronized JSONObject readPackIndex() {
        try {
            File f = packIndexFile();
            if (!f.isFile()) return new JSONObject();
            return new JSONObject(new String(readAll(f), "UTF-8"));
        } catch (Exception e) {
            return new JSONObject();
        }
    }

    private synchronized void writePackIndex(JSONObject o) {
        try {
            writeAtomic(packIndexFile(), o.toString().getBytes("UTF-8"));
        } catch (Exception e) {
            Log.w("YehorivkaMap", "index write failed", e);
        }
    }

    private static String hex(byte[] d) {
        StringBuilder b = new StringBuilder(d.length * 2);
        for (byte x : d) {
            b.append(Character.forDigit((x >> 4) & 0xF, 16));
            b.append(Character.forDigit(x & 0xF, 16));
        }
        return b.toString();
    }

    private void jsCall(String js) {
        runOnUiThread(() -> runJs(js));
    }

    /** 原生下载数据包：边下边算 SHA-256，校验大小与哈希后再原子替换，进度回给页面。 */
    private void downloadPack(final String slug, final String url, final String sha,
                              final String sizeText, final String ver, final String reqId) {
        new Thread(() -> {
            final String key = slugOf(slug);
            File tmp = new File(packDir(), key + ".part");
            String err = null;
            long total = 0;
            long size = 0;
            try {
                size = Long.parseLong(sizeText == null ? "0" : sizeText.trim());
            } catch (Exception ignored) {
            }
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(30000);
                c.setInstanceFollowRedirects(true);
                c.setRequestProperty("User-Agent", "YehorivkaMap/" + ver);
                int code = c.getResponseCode();
                if (code != 200) throw new Exception("HTTP " + code);
                long len = c.getContentLength();
                if (len > MAX_PACK_BYTES) throw new Exception("数据包过大");
                MessageDigest md = MessageDigest.getInstance("SHA-256");
                int lastPct = -1;
                try (InputStream in = c.getInputStream(); FileOutputStream out = new FileOutputStream(tmp)) {
                    byte[] buf = new byte[65536];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                        md.update(buf, 0, n);
                        total += n;
                        if (total > MAX_PACK_BYTES) throw new Exception("数据包过大");
                        if (len > 0) {
                            int pct = (int) (total * 100 / len);
                            if (pct != lastPct && (pct % 2 == 0 || pct >= 100)) {
                                lastPct = pct;
                                final int p = Math.min(100, pct);
                                jsCall("window.__mapDl && __mapDl(" + JSONObject.quote(reqId) + ",1," + p + ")");
                            }
                        }
                    }
                }
                String got = hex(md.digest());
                if (sha != null && sha.length() == 64 && !got.equalsIgnoreCase(sha)) throw new Exception("校验失败（SHA256 不符）");
                if (size > 0 && total != size) throw new Exception("大小不符");
                File dst = packFile(slug);
                if (dst.exists() && !dst.delete()) throw new Exception("覆盖不了旧数据");
                if (!tmp.renameTo(dst)) throw new Exception("写入失败");
                JSONObject idx = readPackIndex();
                JSONObject ent = new JSONObject();
                ent.put("v", ver);
                ent.put("size", total);
                ent.put("t", System.currentTimeMillis() / 1000);
                idx.put(key, ent);
                writePackIndex(idx);
                Log.d("YehorivkaMap", "pack saved " + key + " " + total + " bytes v=" + ver);
            } catch (Exception e) {
                err = (e.getMessage() == null) ? e.toString() : e.getMessage();
                if (tmp.exists() && !tmp.delete()) Log.w("YehorivkaMap", "cannot delete part file");
                Log.w("YehorivkaMap", "pack download failed: " + err, e);
            }
            final String e2 = err;
            jsCall("window.__mapDl && __mapDl(" + JSONObject.quote(reqId) + ","
                    + (e2 == null ? 2 : 0) + "," + JSONObject.quote(e2 == null ? ver : e2) + ")");
        }).start();
    }

    /** 小文本 GET（manifest.json）：同样走原生，避开 file:// 页面的跨域限制。 */
    private void fetchText(final String url, final String reqId) {
        new Thread(() -> {
            String body = null;
            String err = null;
            try {
                HttpURLConnection c = (HttpURLConnection) new URL(url).openConnection();
                c.setConnectTimeout(15000);
                c.setReadTimeout(30000);
                c.setInstanceFollowRedirects(true);
                int code = c.getResponseCode();
                if (code != 200) throw new Exception("HTTP " + code);
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                try (InputStream in = c.getInputStream()) {
                    byte[] buf = new byte[16384];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        bos.write(buf, 0, n);
                        if (bos.size() > MAX_TEXT_BYTES) throw new Exception("内容过大");
                    }
                }
                body = bos.toString("UTF-8");
            } catch (Exception e) {
                err = (e.getMessage() == null) ? e.toString() : e.getMessage();
            }
            final String b2 = body;
            final String e2 = err;
            jsCall("window.__netGet && __netGet(" + JSONObject.quote(reqId) + ","
                    + (b2 != null ? 1 : 0) + "," + JSONObject.quote(b2 == null ? "" : b2) + ","
                    + JSONObject.quote(e2 == null ? "" : e2) + ")");
        }).start();
    }

    private static String safeName(String name) {
        if (name == null || name.trim().isEmpty()) {
            return "yehorivka-archive.json";
        }
        String s = name.trim().replaceAll("[\\\\/:*?\"<>|\\s]+", "_");
        if (!s.toLowerCase().endsWith(".json")) {
            s = s + ".json";
        }
        return s;
    }

    /** Saves the archive to the phone, then opens the system share sheet (QQ / WeChat / …). */
    private void doExport(String rawName, String json) {
        final String name = safeName(rawName);
        Uri shareUri = null;
        String savedPath = null;

        // 1) always keep a private copy (no permission needed)
        try {
            File dir = new File(getExternalFilesDir(null), "archives");
            if (!dir.exists()) dir.mkdirs();
            File f = new File(dir, name);
            try (OutputStream os = new FileOutputStream(f)) {
                os.write(json.getBytes("UTF-8"));
            }
            savedPath = f.getAbsolutePath();
        } catch (Exception e) {
            Log.w("YehorivkaMap", "private archive copy failed", e);
        }

        // 2) Android 10+: real file in 下载/Yehorivka, shareable via content://
        if (Build.VERSION.SDK_INT >= 29) {
            try {
                ContentValues cv = new ContentValues();
                cv.put(MediaStore.MediaColumns.DISPLAY_NAME, name);
                cv.put(MediaStore.MediaColumns.MIME_TYPE, "application/json");
                cv.put(MediaStore.MediaColumns.RELATIVE_PATH, Environment.DIRECTORY_DOWNLOADS + "/Yehorivka");
                Uri uri = getContentResolver().insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, cv);
                if (uri != null) {
                    try (OutputStream os = getContentResolver().openOutputStream(uri)) {
                        if (os != null) os.write(json.getBytes("UTF-8"));
                    }
                    ContentValues done = new ContentValues();
                    done.put(MediaStore.MediaColumns.IS_PENDING, 0);
                    getContentResolver().update(uri, done, null, null);
                    shareUri = uri;
                }
            } catch (Exception e) {
                Log.w("YehorivkaMap", "MediaStore export failed", e);
            }
        }

        if (shareUri != null) {
            Intent send = new Intent(Intent.ACTION_SEND);
            send.setType("application/json");
            send.putExtra(Intent.EXTRA_STREAM, shareUri);
            send.putExtra(Intent.EXTRA_SUBJECT, name);
            send.putExtra(Intent.EXTRA_TEXT, "Squad 地图存档：" + name);
            send.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION);
            try {
                startActivity(Intent.createChooser(send, "导出存档 · 发送到"));
                Toast.makeText(this, "已保存到 下载/Yehorivka/" + name, Toast.LENGTH_LONG).show();
                return;
            } catch (Exception e) {
                Log.w("YehorivkaMap", "share failed", e);
            }
        }

        // 3) fallback: no shareable uri -> share the JSON text itself
        Intent sendText = new Intent(Intent.ACTION_SEND);
        sendText.setType("text/plain");
        sendText.putExtra(Intent.EXTRA_SUBJECT, name);
        sendText.putExtra(Intent.EXTRA_TEXT, json);
        try {
            startActivity(Intent.createChooser(sendText, "导出存档 · 发送到"));
            Toast.makeText(this, savedPath != null ? ("存档已保存：" + savedPath) : "存档已导出", Toast.LENGTH_LONG).show();
        } catch (Exception e) {
            Toast.makeText(this, "导出失败：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    private void doPickArchive() {
        Intent i = new Intent(Intent.ACTION_OPEN_DOCUMENT);
        i.addCategory(Intent.CATEGORY_OPENABLE);
        i.setType("*/*");
        i.putExtra(Intent.EXTRA_MIME_TYPES, new String[]{"application/json", "text/plain", "application/octet-stream"});
        try {
            startActivityForResult(Intent.createChooser(i, "选择存档文件"), REQ_IMPORT);
        } catch (Exception e) {
            Toast.makeText(this, "打不开文件选择器：" + e.getMessage(), Toast.LENGTH_LONG).show();
        }
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != REQ_IMPORT) return;
        if (resultCode != RESULT_OK || data == null || data.getData() == null) {
            runJs("window.__yhImport && archHint && archHint('已取消导入。');");
            return;
        }
        final Uri uri = data.getData();
        new Thread(() -> {
            String text = null;
            String err = null;
            try (InputStream in = getContentResolver().openInputStream(uri)) {
                if (in == null) throw new Exception("打不开文件");
                ByteArrayOutputStream bos = new ByteArrayOutputStream();
                byte[] buf = new byte[8192];
                int n, total = 0;
                while ((n = in.read(buf)) > 0) {
                    total += n;
                    if (total > MAX_ARCHIVE_BYTES) throw new Exception("文件过大");
                    bos.write(buf, 0, n);
                }
                text = bos.toString("UTF-8");
            } catch (Exception e) {
                err = e.getMessage() == null ? e.toString() : e.getMessage();
            }
            final String payload = text;
            final String error = err;
            runOnUiThread(() -> {
                if (payload == null) {
                    Toast.makeText(this, "读取失败：" + error, Toast.LENGTH_LONG).show();
                } else {
                    runJs("window.__yhImport(" + JSONObject.quote(payload) + ")");
                }
            });
        }).start();
    }

    private void runJs(String js) {
        if (webView != null) {
            webView.evaluateJavascript(js, null);
        }
    }

    private void applySystemBars() {
        Window window = getWindow();
        window.setStatusBarColor(Color.parseColor("#0B0F14"));
        window.setNavigationBarColor(Color.parseColor("#0B0F14"));
        if (Build.VERSION.SDK_INT >= 29) {
            window.setNavigationBarContrastEnforced(false);
        }
        if (Build.VERSION.SDK_INT >= 30) {
            window.setDecorFitsSystemWindows(false);
        } else {
            window.getDecorView().setSystemUiVisibility(
                    View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                            | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                            | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION);
        }
        View root = findViewById(R.id.root);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(0, insets.getSystemWindowInsetTop(), 0, insets.getSystemWindowInsetBottom());
            return insets;
        });
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
            return;
        }
        super.onBackPressed();
    }

    @Override
    protected void onPause() {
        if (webView != null) {
            webView.onPause();
        }
        super.onPause();
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) {
            webView.onResume();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.loadUrl("about:blank");
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
