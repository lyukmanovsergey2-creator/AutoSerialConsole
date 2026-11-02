[app]
title = AutoSerialConsole
package.name = autoserialconsole
package.domain = org.scan.kom

source.dir = .
source.include_exts = py,png,jpg,kv,atlas
# Тесты не нужны внутри APK.
source.exclude_dirs = tests

version = 1.2
# Android versionCode. Должен расти с каждым релизом, иначе система
# откажется устанавливать обновление поверх старой версии.
android.numeric_version = 120

# Kivy + USB-OTG serial (CH34x/CP210x/FTDI/PL2303) + pyjnius для Android API.
# Версии usb4a/usbserial4a зафиксированы для воспроизводимых сборок.
requirements = python3,kivy==2.3.0,usb4a==0.3.0,usbserial4a==0.4.0,pyjnius

# Фиксируем python-for-android на релиз, собирающий целевой Python 3.11:
# в 3.13 изменилась сигнатура _PyLong_AsByteArray, и готовые .c-файлы Kivy 2.3.0
# (сгенерированы старым Cython) не компилируются под 3.13.
p4a.branch = v2024.01.21

orientation = portrait
fullscreen = 0

# Доступ к USB-устройству (OTG-переходнику) приложение запрашивает в рантайме
# через usb4a (системный диалог при первом подключении).
# Постоянные permissions не нужны: INTERNET приложению не требуется.
# android.features намеренно не задаём: p4a v2024.01.21 не поддерживает --feature
# (buildozer 1.5.0 передаёт его при непустом значении, и сборка падает).
#android.permissions =

android.api = 34
android.minapi = 21
# NDK 25b — его clang менее строг (нужно для сборки Kivy 2.3.0)
android.ndk = 25b
# Пока только arm64-v8a: покрывает практически все современные телефоны,
# сборка вдвое быстрее. armeabi-v7a можно добавить позже.
android.archs = arm64-v8a
android.allow_backup = True
# Автоматически принимать лицензии Android SDK (для CI без интерактива).
android.accept_sdk_license = True
# Не давать экрану гаснуть во время работы с консолью (добавляет WAKE_LOCK).
android.wakelock = True

[buildozer]
log_level = 2
warn_on_root = 0
