# Android APK

Android-оболочка Пирожкового Диспетчера. Она открывает `https://sirius.rusanoff.ru/`, сохраняет снимки успешно открытых страниц и показывает их без сети с плашкой «Оффлайн режим».

## Сборка

1. Открой папку `android` в Android Studio.
2. Выбери встроенный JDK Android Studio 21 и Android SDK Platform 36. Проект использует Gradle Wrapper 8.10.2, Android Studio не должна подменять его другой версией.
3. Выполни `Build > Build APK(s)`.

Готовый debug APK: `app/build/outputs/apk/debug/app-debug.apk`.

Из PowerShell в папке `android` можно собрать так: `./gradlew.bat assembleDebug`.
