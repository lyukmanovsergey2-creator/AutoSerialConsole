[app]
title = AutoSerialConsole
package.name = autoserialconsole
package.domain = org.scan.kom

source.dir = .
source.include_exts = py,png,jpg,kv,atlas

version = 1.0

# Kivy + USB-OTG serial (CH34x/CP210x/FTDI/PL2303) + pyjnius для Android API
requirements = python3,kivy==2.3.0,usb4a,usbserial4a,pyjnius

orientation = portrait
fullscreen = 0

# USB Host для работы с OTG-переходником. Доступ к конкретному устройству
# приложение запрашивает в рантайме (системный диалог при первом подключении).
android.permissions = INTERNET
android.features = android.hardware.usb.host

android.api = 34
android.minapi = 21
android.archs = arm64-v8a, armeabi-v7a
android.allow_backup = 1

[buildozer]
log_level = 2
warn_on_root = 0
