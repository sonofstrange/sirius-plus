package ru.sonofstrange.siriusplus;

import android.Manifest;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;

import androidx.core.content.ContextCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

final class MobileNotifier {
    private static final String EVENTS_CHANNEL = "sirius_events";
    private static final String ALARM_CHANNEL = "sirius_alarm";
    private static String lastFingerprint = "";
    private static long lastShownAt;

    private MobileNotifier() {}

    static void createChannels(Context context) {
        NotificationManager manager = context.getSystemService(NotificationManager.class);
        NotificationChannel events = new NotificationChannel(
            EVENTS_CHANNEL, "События Sirius", NotificationManager.IMPORTANCE_HIGH
        );
        events.setDescription("Напоминания и изменения расписания");
        events.enableVibration(true);

        NotificationChannel alarm = new NotificationChannel(
            ALARM_CHANNEL, "Тревога", NotificationManager.IMPORTANCE_HIGH
        );
        alarm.setDescription("Сигналы угрозы атаки БПЛА");
        alarm.enableVibration(true);
        alarm.setVibrationPattern(new long[]{0, 700, 200, 700, 200, 1000});
        alarm.setSound(
            RingtoneManager.getDefaultUri(RingtoneManager.TYPE_ALARM),
            new AudioAttributes.Builder().setUsage(AudioAttributes.USAGE_ALARM).build()
        );
        manager.createNotificationChannel(events);
        manager.createNotificationChannel(alarm);
    }

    static synchronized void show(Context context, String title, String body, boolean isAlarm) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && ContextCompat.checkSelfPermission(context, Manifest.permission.POST_NOTIFICATIONS)
                != PackageManager.PERMISSION_GRANTED) {
            return;
        }
        String fingerprint = title + "\n" + body + "\n" + isAlarm;
        long now = System.currentTimeMillis();
        if (fingerprint.equals(lastFingerprint) && now - lastShownAt < 4000) return;
        lastFingerprint = fingerprint;
        lastShownAt = now;
        createChannels(context);
        Intent intent = new Intent(context, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent pendingIntent = PendingIntent.getActivity(
            context, 0, intent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        NotificationCompat.Builder builder = new NotificationCompat.Builder(
            context, isAlarm ? ALARM_CHANNEL : EVENTS_CHANNEL
        )
            .setSmallIcon(R.drawable.sirius_logo)
            .setContentTitle(title)
            .setContentText(body)
            .setStyle(new NotificationCompat.BigTextStyle().bigText(body))
            .setContentIntent(pendingIntent)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setCategory(isAlarm ? NotificationCompat.CATEGORY_ALARM : NotificationCompat.CATEGORY_EVENT)
            .setAutoCancel(true);
        NotificationManagerCompat.from(context).notify((int) now, builder.build());
    }
}
