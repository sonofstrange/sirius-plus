package ru.sonofstrange.siriusplus;

import android.content.Context;

import com.google.firebase.messaging.FirebaseMessagingService;
import com.google.firebase.messaging.RemoteMessage;

import java.util.Map;

public class SiriusMessagingService extends FirebaseMessagingService {
    @Override
    public void onNewToken(String token) {
        super.onNewToken(token);
        getSharedPreferences(MainActivity.PUSH_PREFS, Context.MODE_PRIVATE)
            .edit().putString(MainActivity.FCM_TOKEN_KEY, token).apply();
    }

    @Override
    public void onMessageReceived(RemoteMessage message) {
        Map<String, String> data = message.getData();
        String title = data.getOrDefault("title", "Пирожковый Диспетчер");
        String body = data.getOrDefault("body", "Новое уведомление");
        boolean isAlarm = "1".equals(data.get("is_alarm"));
        if (!MainActivity.isAppForeground()) {
            MobileNotifier.show(this, title, body, isAlarm);
        }
    }
}
