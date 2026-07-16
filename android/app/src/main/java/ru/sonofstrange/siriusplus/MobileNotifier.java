package ru.sonofstrange.siriusplus;

import android.Manifest;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.media.AudioAttributes;
import android.media.RingtoneManager;
import android.os.Build;
import android.provider.Settings;

import androidx.core.content.ContextCompat;
import androidx.core.app.NotificationCompat;
import androidx.core.app.NotificationManagerCompat;

final class MobileNotifier {
    private static final String EVENTS_CHANNEL = "sirius_events";
    private static final String ALARM_PREFS = "sirius_alarm_settings";
    private static final String ALARM_PROFILE_KEY = "alarm_profile";
    private static final String CUSTOM_SOUND_KEY = "custom_sound_uri";
    private static final String CUSTOM_CHANNEL_KEY = "custom_channel_id";
    static final String PROFILE_SIREN = "siren";
    static final String PROFILE_RINGTONE = "ringtone";
    static final String PROFILE_NOTIFICATION = "notification";
    static final String PROFILE_VIBRATION = "vibration";
    static final String PROFILE_CUSTOM = "custom";
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
            getAlarmChannelId(context), "Тревога БПЛА", NotificationManager.IMPORTANCE_HIGH
        );
        alarm.setDescription("Сигналы угрозы атаки БПЛА");
        alarm.enableVibration(true);
        alarm.setVibrationPattern(new long[]{0, 700, 200, 700, 200, 1000});
        Uri sound = getAlarmSound(context);
        if (sound == null) {
            alarm.setSound(null, null);
        } else {
            alarm.setSound(sound, new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build());
        }
        manager.createNotificationChannel(events);
        manager.createNotificationChannel(alarm);
    }

    static void setAlarmProfile(Context context, String profile) {
        SharedPreferences prefs = context.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE);
        SharedPreferences.Editor editor = prefs.edit().putString(ALARM_PROFILE_KEY, profile);
        if (!PROFILE_CUSTOM.equals(profile)) editor.remove(CUSTOM_SOUND_KEY).remove(CUSTOM_CHANNEL_KEY);
        editor.apply();
        createChannels(context);
    }

    static void setCustomAlarmSound(Context context, Uri sound) {
        String channelId = "sirius_alarm_custom_" + System.currentTimeMillis();
        context.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE).edit()
            .putString(ALARM_PROFILE_KEY, PROFILE_CUSTOM)
            .putString(CUSTOM_SOUND_KEY, sound.toString())
            .putString(CUSTOM_CHANNEL_KEY, channelId)
            .apply();
        createChannels(context);
    }

    static int getAlarmProfileIndex(Context context) {
        String profile = context.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE)
            .getString(ALARM_PROFILE_KEY, PROFILE_SIREN);
        return switch (profile) {
            case PROFILE_RINGTONE -> 1;
            case PROFILE_NOTIFICATION -> 2;
            case PROFILE_VIBRATION -> 3;
            default -> 0;
        };
    }

    private static String getAlarmChannelId(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE);
        String profile = prefs.getString(ALARM_PROFILE_KEY, PROFILE_SIREN);
        if (PROFILE_CUSTOM.equals(profile)) {
            return prefs.getString(CUSTOM_CHANNEL_KEY, "sirius_alarm_custom_default");
        }
        return "sirius_alarm_" + profile;
    }

    private static Uri getAlarmSound(Context context) {
        SharedPreferences prefs = context.getSharedPreferences(ALARM_PREFS, Context.MODE_PRIVATE);
        return switch (prefs.getString(ALARM_PROFILE_KEY, PROFILE_SIREN)) {
            case PROFILE_RINGTONE -> RingtoneManager.getDefaultUri(RingtoneManager.TYPE_RINGTONE);
            case PROFILE_NOTIFICATION -> RingtoneManager.getDefaultUri(RingtoneManager.TYPE_NOTIFICATION);
            case PROFILE_VIBRATION -> null;
            case PROFILE_CUSTOM -> {
                String value = prefs.getString(CUSTOM_SOUND_KEY, "");
                yield value.isEmpty() ? Settings.System.DEFAULT_ALARM_ALERT_URI : Uri.parse(value);
            }
            default -> Settings.System.DEFAULT_ALARM_ALERT_URI;
        };
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
            context, isAlarm ? getAlarmChannelId(context) : EVENTS_CHANNEL
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
