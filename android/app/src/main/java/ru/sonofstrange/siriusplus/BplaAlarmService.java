package ru.sonofstrange.siriusplus;

import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.media.AudioAttributes;
import android.media.MediaPlayer;
import android.os.Build;
import android.os.IBinder;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

/** Keeps the selected BPLA alarm playing until the alert is cleared or dismissed. */
public class BplaAlarmService extends Service {
    private static final String CHANNEL_ID = "sirius_alarm_loop";
    private static final int NOTIFICATION_ID = 7391;
    private static final String ACTION_START = "ru.sonofstrange.siriusplus.START_BPLA_ALARM";
    private static final String ACTION_STOP = "ru.sonofstrange.siriusplus.STOP_BPLA_ALARM";
    private MediaPlayer player;

    static void start(Context context) {
        Intent intent = new Intent(context, BplaAlarmService.class).setAction(ACTION_START);
        ContextCompat.startForegroundService(context, intent);
    }

    static void stop(Context context) {
        context.stopService(new Intent(context, BplaAlarmService.class).setAction(ACTION_STOP));
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopAlarm();
            stopSelf();
            return START_NOT_STICKY;
        }
        createChannel();
        startForeground(NOTIFICATION_ID, buildNotification());
        startAlarm();
        return START_NOT_STICKY;
    }

    @Override
    public void onDestroy() {
        stopAlarm();
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    private void startAlarm() {
        if (player != null) return;
        try {
            if (MobileNotifier.getAlarmSound(this) == null) return;
            player = new MediaPlayer();
            player.setAudioAttributes(new AudioAttributes.Builder()
                .setUsage(AudioAttributes.USAGE_ALARM)
                .setContentType(AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build());
            player.setDataSource(this, MobileNotifier.getAlarmSound(this));
            player.setLooping(true);
            player.prepare();
            player.start();
        } catch (Exception ignored) {
            stopAlarm();
        }
    }

    private void stopAlarm() {
        if (player == null) return;
        try { player.stop(); } catch (IllegalStateException ignored) {}
        player.release();
        player = null;
    }

    private void createChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationChannel channel = new NotificationChannel(
            CHANNEL_ID, "Активная тревога БПЛА", NotificationManager.IMPORTANCE_LOW
        );
        channel.setSound(null, null);
        channel.setVibrationPattern(new long[0]);
        getSystemService(NotificationManager.class).createNotificationChannel(channel);
    }

    private android.app.Notification buildNotification() {
        Intent stopIntent = new Intent(this, BplaAlarmService.class).setAction(ACTION_STOP);
        PendingIntent stopPendingIntent = PendingIntent.getService(
            this, 0, stopIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        Intent openIntent = new Intent(this, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_CLEAR_TOP | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        PendingIntent openPendingIntent = PendingIntent.getActivity(
            this, 0, openIntent, PendingIntent.FLAG_UPDATE_CURRENT | PendingIntent.FLAG_IMMUTABLE
        );
        return new NotificationCompat.Builder(this, CHANNEL_ID)
            .setSmallIcon(R.drawable.sirius_logo)
            .setContentTitle("Тревога БПЛА в Sirius")
            .setContentText("Сирена включена до отбоя угрозы")
            .setContentIntent(openPendingIntent)
            .setOngoing(true)
            .setCategory(NotificationCompat.CATEGORY_ALARM)
            .addAction(0, "Остановить звук", stopPendingIntent)
            .build();
    }
}
