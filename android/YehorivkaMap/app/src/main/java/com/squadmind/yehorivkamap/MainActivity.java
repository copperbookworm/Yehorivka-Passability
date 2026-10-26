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

public class MainActivity extends Activity {
    private static final String URL = "file:///android_asset/maps/yehorivka.html";
    private static final int REQ_IMPORT = 4711;
    private static final int MAX_ARCHIVE_BYTES = 8 * 1024 * 1024;
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
