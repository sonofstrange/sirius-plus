package ru.sonofstrange.siriusplus;

import android.Manifest;
import android.annotation.SuppressLint;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.ConnectivityManager;
import android.net.NetworkCapabilities;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.view.Gravity;
import android.view.View;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.window.OnBackInvokedDispatcher;
import android.widget.Button;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;

import androidx.core.app.ActivityCompat;
import androidx.core.content.ContextCompat;
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout;

import com.google.firebase.FirebaseApp;
import com.google.firebase.messaging.FirebaseMessaging;

import java.io.File;
import java.net.URL;
import java.nio.charset.StandardCharsets;
import java.nio.file.AtomicMoveNotSupportedException;
import java.nio.file.Files;
import java.nio.file.StandardCopyOption;
import java.security.MessageDigest;
import java.util.Locale;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.regex.Matcher;
import java.util.regex.Pattern;

import javax.net.ssl.HttpsURLConnection;

public class MainActivity extends android.app.Activity {
    private static final String APP_URL = "https://sirius.rusanoff.ru/";
    private static final String OFFLINE_PAGE_URL = "file:///android_asset/offline.html";
    private static final String APP_SCHEME = "https";
    private static final String APP_HOST = "sirius.rusanoff.ru";
    private static final String HEALTH_URL = APP_URL + "healthz";
    private static final String OFFLINE_PREFS = "sirius_offline";
    private static final String OFFLINE_DATA_KEY = "cached_account_data";
    private static final String LAST_PRECACHE_AT = "last_precache_at";
    private static final long PRECACHE_INTERVAL_MS = 12L * 60 * 60 * 1000;
    private static final int MAX_OFFLINE_DATA_BYTES = 250_000;
    private static final String[] PRECACHE_ROUTES = {
        "/", "/events?tab=register", "/events?tab=my&sub=current", "/events?tab=my&sub=watch",
        "/schedule", "/custom-events", "/coins-info", "/coins-info?tab=dronebet",
        "/coins-info?tab=polymarket", "/howto", "/help"
    };
    private static final int NOTIFICATION_PERMISSION_REQUEST = 1001;
    private static final Pattern MATERIAL_ICON_IN_ARCHIVE = Pattern.compile(
        "(<span[^>]*class=3D\"[^\"]*material-symbols-outlined[^\"]*\"[^>]*>)"
            + "((?:[a-z_]|=\\r?\\n)+)(</span>)"
    );
    private static final Pattern SNAPSHOT_LOCATION = Pattern.compile(
        "(?m)^Snapshot-Content-Location: (https?://[^\\r\\n]+)"
    );
    static final String PUSH_PREFS = "sirius_push";
    static final String FCM_TOKEN_KEY = "fcm_token";
    private static volatile boolean appForeground;

    private WebView webView;
    private WebView prefetchWebView;
    private TextView offlineBadge;
    private LinearLayout offlineNotice;
    private LinearLayout offlineLoading;
    private SwipeRefreshLayout swipeRefresh;
    private final ExecutorService probeExecutor = Executors.newSingleThreadExecutor();
    private boolean loadingOfflineSnapshot;
    private boolean serverReachable;
    private boolean offlineMode;
    private String currentAppUrl = APP_URL;
    private String offlineSnapshotUrl;
    private long probeSequence;
    private boolean precaching;
    private int precacheIndex;
    private String prefetchPendingUrl;

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
        settings.setAllowFileAccess(true);
        settings.setAllowContentAccess(false);
        settings.setSafeBrowsingEnabled(true);
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

        prefetchWebView = new WebView(this);
        WebSettings prefetchSettings = prefetchWebView.getSettings();
        prefetchSettings.setJavaScriptEnabled(true);
        prefetchSettings.setDomStorageEnabled(true);
        prefetchSettings.setCacheMode(WebSettings.LOAD_DEFAULT);
        prefetchSettings.setAllowFileAccess(false);
        prefetchSettings.setAllowContentAccess(false);
        prefetchSettings.setSafeBrowsingEnabled(true);
        prefetchWebView.setAlpha(0f);
        prefetchWebView.setWebViewClient(new PrefetchWebViewClient());
        root.addView(prefetchWebView, new FrameLayout.LayoutParams(dp(1), dp(1), Gravity.BOTTOM | Gravity.END));

        offlineLoading = new LinearLayout(this);
        offlineLoading.setOrientation(LinearLayout.VERTICAL);
        offlineLoading.setGravity(Gravity.CENTER);
        offlineLoading.setPadding(dp(32), dp(32), dp(32), dp(32));
        offlineLoading.setBackgroundColor(Color.rgb(237, 238, 242));
        TextView loadingTitle = new TextView(this);
        loadingTitle.setText("Открываю сохранённую страницу");
        loadingTitle.setTextColor(Color.rgb(26, 26, 46));
        loadingTitle.setTextSize(20);
        loadingTitle.setGravity(Gravity.CENTER);
        offlineLoading.addView(loadingTitle, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        TextView loadingHint = new TextView(this);
        loadingHint.setText("Данные уже на устройстве");
        loadingHint.setTextColor(Color.rgb(98, 103, 127));
        loadingHint.setTextSize(14);
        loadingHint.setGravity(Gravity.CENTER);
        loadingHint.setPadding(0, dp(8), 0, 0);
        offlineLoading.addView(loadingHint, new LinearLayout.LayoutParams(
            LinearLayout.LayoutParams.MATCH_PARENT, LinearLayout.LayoutParams.WRAP_CONTENT
        ));
        offlineLoading.setVisibility(View.GONE);
        root.addView(offlineLoading, new FrameLayout.LayoutParams(
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
        noticeText.setText("Оффлайн режим\nМожно смотреть ранее открытые страницы. Запись, обновление и изменения недоступны.");
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
        if (Build.VERSION.SDK_INT >= 33) {
            getOnBackInvokedDispatcher().registerOnBackInvokedCallback(
                OnBackInvokedDispatcher.PRIORITY_DEFAULT, this::handleBackNavigation
            );
        }
        loadApp(appUrlFromIntent());
    }

    @Override
    protected void onResume() {
        super.onResume();
        appForeground = true;
        if (webView != null && offlineMode) probeServer(currentAppUrl);
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
        handleBackNavigation();
    }

    private void handleBackNavigation() {
        if (loadingOfflineSnapshot && offlineSnapshotUrl == null) {
            // Never let an unavailable offline page close the app. If a cache exists,
            // this opens it; otherwise the explanatory page stays on screen.
            openLatestOfflineSnapshot();
            return;
        }
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

    static boolean isAppUrl(String url) {
        if (url == null) return false;
        try {
            Uri parsed = Uri.parse(url);
            int port = parsed.getPort();
            return APP_SCHEME.equalsIgnoreCase(parsed.getScheme())
                && APP_HOST.equalsIgnoreCase(parsed.getHost())
                && (port == -1 || port == 443);
        } catch (Exception ignored) {
            return false;
        }
    }

    private void loadApp(String requestedUrl) {
        String url = isAppUrl(requestedUrl) ? requestedUrl : APP_URL;
        currentAppUrl = url;
        if (hasSnapshot(url) && !isShowingOfflineSnapshot(url)) loadOfflineSnapshot(url);
        probeServer(url);
    }

    private void refreshCurrentPage() {
        probeServer(currentAppUrl, true);
    }

    private void probeServer(String url) {
        probeServer(url, false);
    }

    private void probeServer(String url, boolean forceLiveReload) {
        long sequence = ++probeSequence;
        probeExecutor.execute(() -> {
            boolean reachable = canReachServer();
            runOnUiThread(() -> {
                if (sequence != probeSequence) return;
                serverReachable = reachable;
                swipeRefresh.setRefreshing(false);
                if (reachable) {
                    leaveOfflineMode();
                    if (loadingOfflineSnapshot || forceLiveReload || !url.equals(webView.getUrl())) {
                        loadingOfflineSnapshot = false;
                        offlineSnapshotUrl = null;
                        webView.getSettings().setCacheMode(WebSettings.LOAD_DEFAULT);
                        webView.loadUrl(url);
                    }
                } else {
                    enterOfflineMode();
                    if (!isShowingOfflineSnapshot(url)) loadOfflineSnapshot(url);
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
            int status = connection.getResponseCode();
            // A proxy can block /healthz while the website is still reachable.
            return status >= 200 && status < 500;
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
        hideOfflineLoading();
        setWebContentBottomInset(0);
    }

    private void loadOfflineSnapshot(String requestedUrl) {
        String url = isAppUrl(requestedUrl) ? requestedUrl : currentAppUrl;
        File archive = url == null ? null : findSnapshotArchive(url);
        loadingOfflineSnapshot = true;
        offlineSnapshotUrl = snapshotKey(url);
        showOfflineLoading();
        webView.stopLoading();
        webView.getSettings().setCacheMode(WebSettings.LOAD_CACHE_ONLY);
        if (archive == null || !archive.isFile() || archive.length() == 0) {
            showOfflineUnavailablePage();
            return;
        }
        webView.loadUrl(Uri.fromFile(archive).toString());
    }

    private void showOfflineUnavailablePage() {
        offlineSnapshotUrl = null;
        webView.loadUrl(OFFLINE_PAGE_URL);
    }

    private void showOfflineLoading() {
        offlineLoading.setVisibility(View.VISIBLE);
    }

    private void hideOfflineLoading() {
        offlineLoading.setVisibility(View.GONE);
    }

    private void saveSnapshot(String url) {
        if (!isAppUrl(url) || loadingOfflineSnapshot) return;
        File archive = snapshotArchive(url);
        File temporaryArchive = snapshotTempArchive(url);
        if (temporaryArchive.exists() && !temporaryArchive.delete()) return;
        String archiveStyle = "(function(){const old=document.getElementById('sirius-archive-cleanup');if(old)old.remove();const style=document.createElement('style');style.id='sirius-archive-cleanup';style.textContent='.modal-overlay,.toast-container{display:none!important}';document.head.appendChild(style);})()";
        webView.evaluateJavascript(archiveStyle, ignored -> webView.saveWebArchive(temporaryArchive.getAbsolutePath(), false, savedPath -> {
            replaceSnapshot(temporaryArchive, archive);
            webView.post(() -> webView.evaluateJavascript("(function(){var e=document.getElementById('sirius-archive-cleanup');if(e)e.remove();})()", null));
        }));
    }

    private void startPrecacheIfNeeded() {
        if (precaching || loadingOfflineSnapshot || offlineMode) return;
        long lastPrecache = getSharedPreferences(OFFLINE_PREFS, MODE_PRIVATE).getLong(LAST_PRECACHE_AT, 0);
        if (System.currentTimeMillis() - lastPrecache < PRECACHE_INTERVAL_MS) return;
        webView.evaluateJavascript(
            "fetch('/api/token-status',{credentials:'same-origin'}).then(r=>r.ok).catch(()=>false)",
            result -> {
                if (!"true".equals(result) || precaching || offlineMode) return;
                precaching = true;
                precacheIndex = 0;
                loadNextPrecachePage();
            }
        );
    }

    private void loadNextPrecachePage() {
        if (!precaching) return;
        if (precacheIndex >= PRECACHE_ROUTES.length) {
            precaching = false;
            getSharedPreferences(OFFLINE_PREFS, MODE_PRIVATE).edit()
                .putLong(LAST_PRECACHE_AT, System.currentTimeMillis()).apply();
            return;
        }
        prefetchPendingUrl = APP_URL + PRECACHE_ROUTES[precacheIndex++].replaceFirst("^/", "");
        prefetchWebView.loadUrl(prefetchPendingUrl);
    }

    private void savePrefetchedSnapshot(String url) {
        if (!url.equals(prefetchPendingUrl)) return;
        prefetchPendingUrl = null;
        if (!isAppUrl(url)) {
            loadNextPrecachePage();
            return;
        }
        File archive = snapshotArchive(url);
        File temporaryArchive = snapshotTempArchive(url);
        if (temporaryArchive.exists() && !temporaryArchive.delete()) {
            loadNextPrecachePage();
            return;
        }
        String cleanup = "(function(){const old=document.getElementById('sirius-archive-cleanup');if(old)old.remove();const style=document.createElement('style');style.id='sirius-archive-cleanup';style.textContent='.modal-overlay,.toast-container{display:none!important}';document.head.appendChild(style);})()";
        prefetchWebView.evaluateJavascript(cleanup, ignored -> prefetchWebView.saveWebArchive(
            temporaryArchive.getAbsolutePath(), false, savedPath -> {
                replaceSnapshot(temporaryArchive, archive);
                prefetchWebView.evaluateJavascript("(function(){var e=document.getElementById('sirius-archive-cleanup');if(e)e.remove();})()", null);
                prefetchWebView.postDelayed(this::loadNextPrecachePage, 700);
            }
        ));
    }

    private void replaceArchiveIconNames(File archive) {
        try {
            String content = new String(java.nio.file.Files.readAllBytes(archive.toPath()), StandardCharsets.UTF_8);
            content = content
                .replace("&#128100;", "&#9678;")
                .replace("&#128172;", "&#9993;&#xFE0E;")
                .replace("&#128652;", "&#9646;")
                .replace("&#128276;", "!")
                .replace("&#128241;", "&#9742;&#xFE0E;");
            Matcher matcher = MATERIAL_ICON_IN_ARCHIVE.matcher(content);
            StringBuffer rewritten = new StringBuffer();
            boolean changed = false;
            while (matcher.find()) {
                String name = matcher.group(2).replaceAll("=\\r?\\n", "");
                String replacement = offlineIcon(name);
                if (replacement == null) {
                    matcher.appendReplacement(rewritten, Matcher.quoteReplacement(matcher.group()));
                    continue;
                }
                changed = true;
                matcher.appendReplacement(rewritten, Matcher.quoteReplacement(
                    matcher.group(1) + replacement + matcher.group(3)
                ));
            }
            matcher.appendTail(rewritten);
            String normalized = rewritten.toString();
            if (!normalized.contains("data-sirius-offline-icons=3D\"1\"")) {
                normalized = normalized.replace(
                    "class=3D\"material-symbols-outlined\"",
                    "class=3D\"material-symbols-outlined\" data-sirius-offline-icons=3D\"1\" style=3D\"font-family:system-ui,sans-serif;font-feature-settings:normal\""
                );
            }
            if (changed || !normalized.equals(content)) {
                java.nio.file.Files.write(archive.toPath(), normalized.getBytes(StandardCharsets.UTF_8));
            }
        } catch (Exception ignored) {
            // A damaged offline archive is handled by the normal offline fallback.
        }
    }

    private String offlineIcon(String name) {
        return switch (name) {
            case "account_circle", "person" -> "&#9678;";
            case "add" -> "+";
            case "add_chart" -> "&#9636;";
            case "admin_panel_settings" -> "&#9881;&#xFE0E;";
            case "alarm" -> "&#9201;&#xFE0E;";
            case "block", "visibility_off" -> "&#8856;";
            case "calendar_month" -> "&#9638;";
            case "chat_bubble_outline" -> "&#9993;&#xFE0E;";
            case "close" -> "&times;";
            case "dark_mode" -> "&#9680;";
            case "delete" -> "&#9003;";
            case "directions_bus_filled" -> "&#9646;";
            case "error" -> "!";
            case "help_outline" -> "?";
            case "location_on" -> "&#8982;";
            case "menu" -> "&#9776;";
            case "new_releases" -> "&#10022;";
            case "notifications", "notifications_active" -> "!";
            case "paid" -> "&#8381;";
            case "phone_android" -> "&#9742;&#xFE0E;";
            case "query_stats" -> "&#9636;";
            case "radar" -> "&#9673;";
            case "schedule" -> "&#9687;";
            case "search" -> "&#8981;";
            case "sync" -> "&#8635;";
            case "sync_alt" -> "&#8596;";
            case "verified" -> "&#10003;";
            case "warning" -> "&#9888;";
            default -> null;
        };
    }

    private void prepareOfflineSnapshot() {
        String cachedData = getSharedPreferences(OFFLINE_PREFS, MODE_PRIVATE)
            .getString(OFFLINE_DATA_KEY, "");
        String escapedData = org.json.JSONObject.quote(cachedData);
        webView.evaluateJavascript("""
            (function(){
                if(window.__siriusOfflinePrepared)return;
                window.__siriusOfflinePrepared=true;
                // Navigation, the menu and the theme switcher are safe offline. Only
                // form submission is stopped because it would require the server.
                document.addEventListener('submit',event=>event.preventDefault(),true);
                let cached={};
                try{cached=JSON.parse(%s)||{};}catch(e){}
                const originalFetch=window.fetch.bind(window);
                window.fetch=function(input,init){
                    const url=typeof input==='string'?input:(input&&input.url)||'';
                    let response=null;
                    if(url.includes('/api/user-info'))response=cached.userInfo;
                    else if(url.includes('/api/coins/balance'))response=cached.coins;
                    else if(url.includes('/api/notifications/history'))response=cached.notificationHistory;
                    if(response)return Promise.resolve({ok:true,status:200,json:()=>Promise.resolve(response)});
                    return originalFetch(input,init);
                };
            })();
        """.formatted(escapedData), null);
    }

    private boolean hasSnapshot(String url) {
        File archive = findSnapshotArchive(url);
        return archive != null && archive.isFile() && archive.length() > 0;
    }

    private boolean isShowingOfflineSnapshot(String url) {
        return loadingOfflineSnapshot && snapshotKey(url).equals(offlineSnapshotUrl);
    }

    private File snapshotArchive(String url) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(snapshotKey(url).getBytes(StandardCharsets.UTF_8));
            StringBuilder fileName = new StringBuilder("page_v5_");
            for (byte value : digest) fileName.append(String.format("%02x", value));
            return new File(getFilesDir(), fileName.append(".mht").toString());
        } catch (Exception ignored) {
            return new File(getFilesDir(), "page_fallback.mht");
        }
    }

    private File snapshotTempArchive(String url) {
        return new File(snapshotArchive(url).getPath() + ".tmp");
    }

    private boolean replaceSnapshot(File temporaryArchive, File archive) {
        if (!temporaryArchive.isFile() || temporaryArchive.length() == 0) return false;
        try {
            Files.move(temporaryArchive.toPath(), archive.toPath(),
                StandardCopyOption.ATOMIC_MOVE, StandardCopyOption.REPLACE_EXISTING);
            return true;
        } catch (AtomicMoveNotSupportedException ignored) {
            try {
                Files.move(temporaryArchive.toPath(), archive.toPath(), StandardCopyOption.REPLACE_EXISTING);
                return true;
            } catch (Exception ignoredAgain) {
                return false;
            }
        } catch (Exception ignored) {
            return false;
        }
    }

    private File findSnapshotArchive(String url) {
        File current = snapshotArchive(url);
        if (current.isFile() && current.length() > 0) return current;

        File[] archives = getFilesDir().listFiles((dir, name) -> name.startsWith("page_v5_") && name.endsWith(".mht"));
        if (archives == null) return null;
        String key = snapshotKey(url);
        for (File archive : archives) {
            try {
                String header = new String(java.nio.file.Files.readAllBytes(archive.toPath()), StandardCharsets.UTF_8);
                Matcher location = SNAPSHOT_LOCATION.matcher(header);
                if (location.find() && key.equals(snapshotKey(location.group(1)))) return archive;
            } catch (Exception ignored) {
                // Ignore a damaged cached page and keep looking for another copy.
            }
        }
        return null;
    }

    private boolean openLatestOfflineSnapshot() {
        File[] archives = getFilesDir().listFiles((dir, name) -> name.startsWith("page_v5_") && name.endsWith(".mht"));
        if (archives == null || archives.length == 0) return false;
        File latest = null;
        for (File archive : archives) {
            if (archive.length() > 0 && (latest == null || archive.lastModified() > latest.lastModified())) latest = archive;
        }
        if (latest == null) return false;
        String url = snapshotSourceUrl(latest);
        currentAppUrl = url == null ? APP_URL : url;
        loadingOfflineSnapshot = true;
        offlineSnapshotUrl = snapshotKey(currentAppUrl);
        showOfflineLoading();
        webView.getSettings().setCacheMode(WebSettings.LOAD_CACHE_ONLY);
        webView.loadUrl(Uri.fromFile(latest).toString());
        return true;
    }

    private String snapshotSourceUrl(File archive) {
        try {
            String content = new String(java.nio.file.Files.readAllBytes(archive.toPath()), StandardCharsets.UTF_8);
            Matcher location = SNAPSHOT_LOCATION.matcher(content);
            return location.find() ? location.group(1) : null;
        } catch (Exception ignored) {
            return null;
        }
    }

    private File legacySnapshotArchive(String url) {
        try {
            byte[] digest = MessageDigest.getInstance("SHA-256")
                .digest(url.getBytes(StandardCharsets.UTF_8));
            StringBuilder fileName = new StringBuilder("page_");
            for (byte value : digest) fileName.append(String.format("%02x", value));
            return new File(getFilesDir(), fileName.append(".mht").toString());
        } catch (Exception ignored) {
            return new File(getFilesDir(), "page_fallback.mht");
        }
    }

    private String snapshotKey(String url) {
        if (url == null) return APP_URL;
        Uri parsed = Uri.parse(url);
        String path = parsed.getPath();
        if (path == null || path.isEmpty()) path = "/";
        if (path.length() > 1 && path.endsWith("/")) path = path.substring(0, path.length() - 1);
        String normalizedPath = path.substring(1).toLowerCase(Locale.ROOT);
        if ("coins-info".equals(normalizedPath) && "dronebet".equals(parsed.getQueryParameter("tab"))) {
            return APP_URL + "coins-info?tab=dronebet";
        }
        StringBuilder key = new StringBuilder(APP_URL).append(normalizedPath);
        if ("events".equals(normalizedPath)) {
            String tab = parsed.getQueryParameter("tab");
            String sub = parsed.getQueryParameter("sub");
            String status = parsed.getQueryParameter("status");
            if (tab != null) key.append("?tab=").append(tab);
            if (sub != null) key.append(key.indexOf("?") < 0 ? "?sub=" : "&sub=").append(sub);
            if (status != null) key.append(key.indexOf("?") < 0 ? "?status=" : "&status=").append(status);
        } else if ("schedule".equals(normalizedPath)) {
            String date = parsed.getQueryParameter("date");
            if (date != null) key.append("?date=").append(date);
        } else if ("coins-info".equals(normalizedPath)) {
            String tab = parsed.getQueryParameter("tab");
            if (tab != null) key.append("?tab=").append(tab);
        }
        return key.toString();
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
        try {
            FirebaseApp firebaseApp = FirebaseApp.initializeApp(this);
            if (firebaseApp == null) return;
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

    private void cacheOfflineAccountData() {
        if (loadingOfflineSnapshot || !isAppUrl(webView.getUrl())) return;
        webView.evaluateJavascript("""
            (async function(){
                if(!window.SiriusAndroid)return;
                try{
                    const [userInfo,coins,notificationHistory]=await Promise.all([
                        fetch('/api/user-info',{credentials:'same-origin'}).then(r=>r.json()),
                        fetch('/api/coins/balance',{credentials:'same-origin'}).then(r=>r.json()),
                        fetch('/api/notifications/history',{credentials:'same-origin'}).then(r=>r.json())
                    ]);
                    if(userInfo&&userInfo.ok){
                        window.SiriusAndroid.cacheOfflineAccountData(JSON.stringify({userInfo,coins,notificationHistory}));
                    }
                }catch(e){}
            })();
        """, null);
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
            if (isAppForeground() && isAppUrl(webView.getUrl())) {
                runOnUiThread(() -> showNativeNotification(message));
            }
        }

        @JavascriptInterface
        public void openLastSavedPage() {
            runOnUiThread(() -> openLatestOfflineSnapshot());
        }

        @JavascriptInterface
        public void retryConnection() {
            runOnUiThread(() -> probeServer(currentAppUrl, true));
        }

        @JavascriptInterface
        public void cacheOfflineAccountData(String payload) {
            if (payload == null || payload.getBytes(StandardCharsets.UTF_8).length > MAX_OFFLINE_DATA_BYTES) return;
            runOnUiThread(() -> {
                if (webView != null && isAppUrl(webView.getUrl())) {
                    getSharedPreferences(OFFLINE_PREFS, MODE_PRIVATE).edit()
                        .putString(OFFLINE_DATA_KEY, payload).apply();
                }
            });
        }
    }

    private final class PrefetchWebViewClient extends WebViewClient {
        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            if (!precaching) return;
            view.postDelayed(() -> savePrefetchedSnapshot(url), 1500);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (request.isForMainFrame()) {
                prefetchPendingUrl = null;
                view.postDelayed(MainActivity.this::loadNextPrecachePage, 700);
            }
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
            currentAppUrl = url;
            if (offlineMode) {
                enterOfflineMode();
                loadOfflineSnapshot(url);
                return true;
            }
            return false;
        }

        @Override
        public void onPageStarted(WebView view, String url, android.graphics.Bitmap favicon) {
            if (!loadingOfflineSnapshot && !isAppUrl(url)) {
                view.stopLoading();
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse(url)));
                return;
            }
            super.onPageStarted(view, url, favicon);
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            super.onPageFinished(view, url);
            swipeRefresh.setRefreshing(false);
            if (loadingOfflineSnapshot) {
                // The fallback itself must keep its native "return" button active.
                if (!url.startsWith(OFFLINE_PAGE_URL)) prepareOfflineSnapshot();
                return;
            }
            if (isAppUrl(url)) currentAppUrl = url;
            leaveOfflineMode();
            saveSnapshot(url);
            startPrecacheIfNeeded();
            installNativeNotificationBridge();
            syncFcmToken();
            cacheOfflineAccountData();
            view.evaluateJavascript(
                "fetch('/api/app-bonus', {method: 'POST', credentials: 'same-origin'}).catch(function() {})",
                null
            );
        }

        @Override
        public void onPageCommitVisible(WebView view, String url) {
            super.onPageCommitVisible(view, url);
            if (loadingOfflineSnapshot) hideOfflineLoading();
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame()) return;
            if (loadingOfflineSnapshot) {
                if (!OFFLINE_PAGE_URL.equals(request.getUrl().toString())) showOfflineUnavailablePage();
            } else {
                showOfflineFallback(request.getUrl().toString());
            }
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (request.isForMainFrame() && response.getStatusCode() >= 500 && !loadingOfflineSnapshot) {
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
        if (isAppUrl(url)) currentAppUrl = url;
        enterOfflineMode();
        if (!isShowingOfflineSnapshot(currentAppUrl)) loadOfflineSnapshot(currentAppUrl);
    }
}
