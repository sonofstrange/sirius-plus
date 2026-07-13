package ru.sonofstrange.siriusplus;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Base64;
import android.view.Gravity;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import com.google.firebase.FirebaseApp;
import com.google.firebase.messaging.FirebaseMessaging;

import org.json.JSONArray;

import java.io.File;
import java.io.FileOutputStream;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import javax.net.ssl.HttpsURLConnection;

public class MainActivity extends android.app.Activity {
    private static final String APP_URL = "https://sirius.rusanoff.ru/";
    private static final String HEALTH_URL = APP_URL + "healthz";
    private static final String LAST_PAGE_URL_FILE = "last_page_url.txt";
    private static final String SNAPSHOT_PREFIX = "SIRIUS_PLUS_SNAPSHOT_V2\n";
    private static final int NOTIFICATION_PERMISSION_REQUEST = 1001;
    static final String PUSH_PREFS = "sirius_push";
    static final String FCM_TOKEN_KEY = "fcm_token";
    private static volatile boolean appForeground;

    private WebView webView;
    private TextView offlineBadge;
    private LinearLayout offlineNotice;
    private SwipeRefreshLayout swipeRefresh;
    private final ExecutorService probeExecutor = Executors.newSingleThreadExecutor();
    private boolean loadingOfflineSnapshot;
    private boolean serverReachable;
    private boolean offlineMode;

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
        webView.addJavascriptInterface(new NativeNotificationBridge(), "SiriusAndroid");
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
        offlineBadge.setTextColor(Color.rgb(45, 49, 56));
        offlineBadge.setTextSize(12);
        offlineBadge.setGravity(Gravity.CENTER);
        offlineBadge.setPadding(dp(12), dp(6), dp(12), dp(6));
        offlineBadge.setBackgroundColor(Color.rgb(220, 223, 228));
        offlineBadge.setVisibility(View.GONE);
        FrameLayout.LayoutParams badgeLayout = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.WRAP_CONTENT, FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM | Gravity.CENTER_HORIZONTAL
        );
        root.addView(offlineBadge, badgeLayout);

        offlineNotice = new LinearLayout(this);
        offlineNotice.setOrientation(LinearLayout.HORIZONTAL);
        offlineNotice.setGravity(Gravity.CENTER_VERTICAL);
        offlineNotice.setPadding(dp(14), dp(10), dp(8), dp(10));
        offlineNotice.setBackgroundColor(Color.rgb(73, 78, 87));
        TextView noticeText = new TextView(this);
        noticeText.setText("Оффлайн режим\nМожно смотреть открытые ранее страницы. Запись, обновление, профиль и коины недоступны.");
        noticeText.setTextColor(Color.WHITE);
        noticeText.setTextSize(13);
        noticeText.setLineSpacing(0, 1.1f);
        offlineNotice.addView(noticeText, new LinearLayout.LayoutParams(0, LinearLayout.LayoutParams.WRAP_CONTENT, 1));
        Button dismissNotice = new Button(this);
        dismissNotice.setText("Понятно");
        dismissNotice.setTextSize(12);
        dismissNotice.setOnClickListener(view -> {
            offlineNotice.setVisibility(View.GONE);
            offlineBadge.setVisibility(View.VISIBLE);
            setWebContentBottomInset(32);
        });
        offlineNotice.addView(dismissNotice, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.WRAP_CONTENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        offlineNotice.setVisibility(View.GONE);
        FrameLayout.LayoutParams noticeLayout = new FrameLayout.LayoutParams(
            FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT,
            Gravity.BOTTOM
        );
        noticeLayout.leftMargin = dp(12);
        noticeLayout.rightMargin = dp(12);
        root.addView(offlineNotice, noticeLayout);
        setContentView(root);

        createNotificationChannel();
        requestNotificationPermission();
        initializeFirebaseMessaging();
        loadApp(appUrlFromIntent());
    }

    @Override
    protected void onResume() {
        super.onResume();
        appForeground = true;
        if (webView != null && offlineMode) probeServer(webView.getUrl());
    }

    @Override
    protected void onPause() {
        appForeground = false;
        super.onPause();
    }

    static boolean isAppForeground() {
        return appForeground;
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        loadApp(appUrlFromIntent());
    }

    @Override
    protected void onDestroy() {
        probeExecutor.shutdownNow();
        super.onDestroy();
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

    private void loadApp(String requestedUrl) {
        String url = isAppUrl(requestedUrl) ? requestedUrl : APP_URL;
        if (hasSnapshot(url)) loadOfflineSnapshot(url);
        probeServer(url);
    }

    private void refreshCurrentPage() {
        loadApp(webView.getUrl());
    }

    private void probeServer(String url) {
        probeExecutor.execute(() -> {
            boolean reachable = canReachServer();
            runOnUiThread(() -> {
                serverReachable = reachable;
                if (reachable) {
                    leaveOfflineMode();
                    loadingOfflineSnapshot = false;
                    webView.getSettings().setCacheMode(WebSettings.LOAD_DEFAULT);
                    webView.loadUrl(url);
                } else {
                    enterOfflineMode();
                    loadOfflineSnapshot(url);
                }
            });
        });
    }

    private boolean canReachServer() {
        HttpsURLConnection connection = null;
        try {
            connection = (HttpsURLConnection) new URL(HEALTH_URL).openConnection();
            connection.setConnectTimeout(2500);
            connection.setReadTimeout(2500);
            connection.setUseCaches(false);
            connection.setRequestMethod("GET");
            return connection.getResponseCode() == 204;
        } catch (Exception ignored) {
            return false;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    private void enterOfflineMode() {
        boolean justEntered = !offlineMode;
        offlineMode = true;
        if (justEntered) {
            offlineBadge.setVisibility(View.GONE);
            offlineNotice.setVisibility(View.VISIBLE);
            setWebContentBottomInset(84);
        } else if (offlineNotice.getVisibility() != View.VISIBLE) {
            offlineBadge.setVisibility(View.VISIBLE);
            setWebContentBottomInset(32);
        }
    }

    private void leaveOfflineMode() {
        offlineMode = false;
        offlineBadge.setVisibility(View.GONE);
        offlineNotice.setVisibility(View.GONE);
        setWebContentBottomInset(0);
    }

    private void loadOfflineSnapshot(String requestedUrl) {
        String url = isAppUrl(requestedUrl) ? requestedUrl : readFile(LAST_PAGE_URL_FILE);
        String html = url == null ? null : readSnapshot(url);
        loadingOfflineSnapshot = true;
        webView.getSettings().setCacheMode(WebSettings.LOAD_CACHE_ELSE_NETWORK);
        webView.stopLoading();
        if (html == null || html.isEmpty()) {
            webView.loadUrl("file:///android_asset/offline.html");
            return;
        }
        webView.loadDataWithBaseURL(url, html, "text/html", "UTF-8", url);
    }

    private void saveSnapshot(String url) {
        if (!isAppUrl(url) || loadingOfflineSnapshot) return;
        String script = "(function(){const copy=document.documentElement.cloneNode(true);copy.querySelectorAll('script').forEach(node=>node.remove());return btoa(unescape(encodeURIComponent(copy.outerHTML));})()";
        webView.evaluateJavascript(script, value -> {
            try {
                String encoded = new JSONArray("[" + value + "]").getString(0);
                String html = new String(Base64.decode(encoded, Base64.DEFAULT), StandardCharsets.UTF_8);
                writeFile(pageFile(url), SNAPSHOT_PREFIX + html);
                writeFile(LAST_PAGE_URL_FILE, url);
            } catch (Exception ignored) {
                // A missing snapshot only affects offline browsing.
            }
        });
    }

    private void disableOfflineActions() {
        webView.evaluateJavascript("""
            (function(){
                const style=document.createElement('style');
                style.textContent='button,input,textarea,select,[onclick]{opacity:.45!important;pointer-events:none!important}';
                document.head.appendChild(style);
                document.querySelectorAll('button,input,textarea,select').forEach(node=>{node.disabled=true;node.title='Недоступно в оффлайн режиме';});
                document.querySelectorAll('form').forEach(form=>form.addEventListener('submit',event=>event.preventDefault()));
            })();
        """, null);
    }

    private boolean hasSnapshot(String url) {
        return readSnapshot(url) != null;
    }

    private String readSnapshot(String url) {
        String stored = readFile(pageFile(url));
        if (stored == null || !stored.startsWith(SNAPSHOT_PREFIX)) return null;
        return stored.substring(SNAPSHOT_PREFIX.length());
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

    private void installNativeNotificationBridge() {
        webView.evaluateJavascript("""
            (function(){
                if(window.__siriusAndroidNotifications)return;
                const container=document.getElementById('notifications-live');
                if(!container || !window.SiriusAndroid)return;
                window.__siriusAndroidNotifications=true;
                container.querySelectorAll('.notification__text').forEach(node=>node.dataset.androidReported='1');
                const report=()=>container.querySelectorAll('.notification__text').forEach(node=>{
                    if(!node.dataset.androidReported){node.dataset.androidReported='1';window.SiriusAndroid.notify(node.textContent);}
                });
                new MutationObserver(report).observe(container,{childList:true,subtree:true});
                report();
            })();
        """, null);
    }

    private void createNotificationChannel() {
        MobileNotifier.createChannels(this);
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) {
            ActivityCompat.requestPermissions(this, new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTIFICATION_PERMISSION_REQUEST);
        }
    }

    private void showNativeNotification(String message) {
        if (Build.VERSION.SDK_INT >= 33 && ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED) return;
        MobileNotifier.show(this, "Пирожковый Диспетчер", message, message.startsWith("🚨"));
    }

    private void initializeFirebaseMessaging() {
        if (getResources().getIdentifier("google_app_id", "string", getPackageName()) == 0) return;
        try {
            FirebaseApp.initializeApp(this);
            FirebaseMessaging.getInstance().getToken().addOnSuccessListener(this::storeFcmToken);
        } catch (Exception ignored) {
            // The application stays usable when Firebase is not configured yet.
        }
    }

    private void storeFcmToken(String token) {
        if (token == null || token.isEmpty()) return;
        getSharedPreferences(PUSH_PREFS, MODE_PRIVATE).edit().putString(FCM_TOKEN_KEY, token).apply();
        syncFcmToken();
    }

    private void syncFcmToken() {
        String token = getSharedPreferences(PUSH_PREFS, MODE_PRIVATE).getString(FCM_TOKEN_KEY, "");
        if (token.isEmpty() || loadingOfflineSnapshot) return;
        String body = org.json.JSONObject.quote(token);
        webView.evaluateJavascript(
            "fetch('/api/mobile/push-token',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:" + body + "})}).catch(function(){})",
            null
        );
    }

    private void setWebContentBottomInset(int bottomDp) {
        FrameLayout.LayoutParams params = (FrameLayout.LayoutParams) swipeRefresh.getLayoutParams();
        params.bottomMargin = dp(bottomDp);
        swipeRefresh.setLayoutParams(params);
    }

    private int dp(int value) {
        return Math.round(value * getResources().getDisplayMetrics().density);
    }

    private final class NativeNotificationBridge {
        @JavascriptInterface
        public void notify(String message) {
            if (isAppForeground()) runOnUiThread(() -> showNativeNotification(message));
        }
    }

    private final class SiriusWebViewClient extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (!isAppUrl(url)) {
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                return true;
            }
            if (!serverReachable) {
                enterOfflineMode();
                loadOfflineSnapshot(url);
                return true;
            }
            return false;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            swipeRefresh.setRefreshing(false);
            if (loadingOfflineSnapshot) {
                disableOfflineActions();
                return;
            }
            leaveOfflineMode();
            saveSnapshot(url);
            installNativeNotificationBridge();
            syncFcmToken();
            view.evaluateJavascript(
                "fetch('/api/app-bonus', {method: 'POST', credentials: 'same-origin'}).catch(function() {})",
                null
            );
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame() && !loadingOfflineSnapshot) {
                showOfflineFallback(request.getUrl().toString());
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame() && response.getStatusCode() >= 400 && !loadingOfflineSnapshot) {
                showOfflineFallback(request.getUrl().toString());
            }
        }

        @Override
        @SuppressWarnings("deprecation")
        public void onReceivedError(WebView view, int errorCode, String description, String failingUrl) {
            if (!loadingOfflineSnapshot && isAppUrl(failingUrl) && failingUrl.equals(view.getUrl())) {
                showOfflineFallback(failingUrl);
            }
        }
    }

    private void showOfflineFallback(String url) {
        serverReachable = false;
        enterOfflineMode();
        loadOfflineSnapshot(url);
    }
}
