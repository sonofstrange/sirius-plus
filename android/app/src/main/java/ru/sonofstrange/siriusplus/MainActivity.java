package ru.sonofstrange.siriusplus;

import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.os.Bundle;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceError;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;

import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import org.json.JSONArray;

import java.io.File;
import java.io.FileOutputStream;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;

public class MainActivity extends android.app.Activity {
    private static final String APP_URL = "https://sirius.rusanoff.ru/";
    private static final String LAST_PAGE_URL_FILE = "last_page_url.txt";

    private WebView webView;
    private TextView offlineBadge;
    private SwipeRefreshLayout swipeRefresh;
    private boolean loadingOfflineSnapshot;

    @Override
    @SuppressLint("SetJavaScriptEnabled")
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        FrameLayout root = new FrameLayout(this);
        root.setOnApplyWindowInsetsListener((view, insets) -> {
            view.setPadding(0, insets.getSystemWindowInsetTop(), 0, insets.getSystemWindowInsetBottom());
            return insets;
        });
        webView = new WebView(this);
        WebSettings settings = webView.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setDatabaseEnabled(true);
        settings.setCacheMode(WebSettings.LOAD_DEFAULT);
        settings.setUserAgentString(settings.getUserAgentString() + " SiriusPlusAndroid/1.0");
        webView.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_YES);
        webView.setWebViewClient(new SiriusWebViewClient());
        swipeRefresh = new SwipeRefreshLayout(this);
        swipeRefresh.setColorSchemeColors(Color.rgb(108, 92, 231));
        swipeRefresh.setOnRefreshListener(this::refreshCurrentPage);
        swipeRefresh.addView(webView, new SwipeRefreshLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
        ));
        root.addView(swipeRefresh, new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.MATCH_PARENT
        ));

        offlineBadge = new TextView(this);
        offlineBadge.setText("Оффлайн режим");
        offlineBadge.setTextColor(Color.WHITE);
        offlineBadge.setTextSize(14);
        offlineBadge.setGravity(Gravity.CENTER);
        offlineBadge.setPadding(dp(16), dp(8), dp(16), dp(8));
        offlineBadge.setBackgroundColor(Color.rgb(49, 101, 190));
        FrameLayout.LayoutParams badgeLayout = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.TOP | Gravity.CENTER_HORIZONTAL
        );
        badgeLayout.topMargin = dp(12);
        root.addView(offlineBadge, badgeLayout);
        setContentView(root);

        loadApp(appUrlFromIntent());
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (webView != null) updateConnectionBadge();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        loadApp(appUrlFromIntent());
    }

    @Override
    public void onBackPressed() {
        if (webView.canGoBack() && !loadingOfflineSnapshot) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    private String appUrlFromIntent() {
        String url = getIntent().getDataString();
        return isAppUrl(url) ? url : APP_URL;
    }

    private boolean isAppUrl(String url) {
        return url != null && url.startsWith(APP_URL);
    }

    private void loadApp(String url) {
        boolean online = isOnline();
        offlineBadge.setVisibility(online ? View.GONE : View.VISIBLE);
        if (online) {
            loadingOfflineSnapshot = false;
            webView.getSettings().setCacheMode(WebSettings.LOAD_DEFAULT);
            webView.loadUrl(isAppUrl(url) ? url : APP_URL);
        } else {
            loadOfflineSnapshot(isAppUrl(url) ? url : null);
        }
    }

    private void refreshCurrentPage() {
        String url = webView.getUrl();
        loadApp(isAppUrl(url) ? url : APP_URL);
    }

    private void updateConnectionBadge() {
        offlineBadge.setVisibility(isOnline() ? View.GONE : View.VISIBLE);
    }

    private boolean isOnline() {
        ConnectivityManager manager = (ConnectivityManager) getSystemService(Context.CONNECTIVITY_SERVICE);
        if (manager == null) return false;
        NetworkCapabilities capabilities = manager.getNetworkCapabilities(manager.getActiveNetwork());
        return capabilities != null && capabilities.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET);
    }

    private void loadOfflineSnapshot(String requestedUrl) {
        String url = requestedUrl == null ? readFile(LAST_PAGE_URL_FILE) : requestedUrl;
        String html = url == null ? null : readFile(pageFile(url));
        loadingOfflineSnapshot = true;
        if (html == null || html.isEmpty()) {
            webView.loadUrl("file:///android_asset/offline.html");
            return;
        }
        webView.getSettings().setCacheMode(WebSettings.LOAD_CACHE_ELSE_NETWORK);
        webView.loadDataWithBaseURL(url, html, "text/html", "UTF-8", url);
    }

    private void saveSnapshot(String url) {
        if (!url.startsWith(APP_URL) || !isOnline()) return;
        String script = "(function(){return btoa(unescape(encodeURIComponent(document.documentElement.outerHTML)));})()";
        webView.evaluateJavascript(script, value -> {
            try {
                String encoded = new JSONArray("[" + value + "]").getString(0);
                String html = new String(Base64.decode(encoded, Base64.DEFAULT), StandardCharsets.UTF_8);
                writeFile(pageFile(url), html);
                writeFile(LAST_PAGE_URL_FILE, url);
            } catch (Exception ignored) {
                // A missing snapshot only affects offline browsing.
            }
        });
    }

    private String readFile(String name) {
        try {
            File file = new File(getFilesDir(), name);
            if (!file.exists()) return null;
            return new String(java.nio.file.Files.readAllBytes(file.toPath()), StandardCharsets.UTF_8);
        } catch (Exception ignored) {
            return null;
        }
    }

    private void writeFile(String name, String content) {
        try (FileOutputStream output = openFileOutput(name, Context.MODE_PRIVATE)) {
            output.write(content.getBytes(StandardCharsets.UTF_8));
        } catch (Exception ignored) {
            // The live site remains usable if the local cache cannot be written.
        }
    }

    private String pageFile(String url) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(url.getBytes(StandardCharsets.UTF_8));
            StringBuilder fileName = new StringBuilder("page_");
            for (byte value : digest) fileName.append(String.format("%02x", value));
            return fileName.append(".html").toString();
        } catch (Exception ignored) {
            return "page_fallback.html";
        }
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private final class SiriusWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (!isAppUrl(url)) return false;
            if (!isOnline()) {
                loadOfflineSnapshot(url);
                return true;
            }
            // Let WebView handle online navigations itself: reloading here turns
            // an HTML form POST into a GET request.
            return false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            swipeRefresh.setRefreshing(false);
            updateConnectionBadge();
            if (!loadingOfflineSnapshot) {
                saveSnapshot(url);
                view.evaluateJavascript(
                    "fetch('/api/app-bonus', {method: 'POST', credentials: 'same-origin'}).catch(function() {})",
                    null
                );
            }
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame() && !isOnline()) {
                loadOfflineSnapshot(request.getUrl().toString());
            }
        }
    }
}
