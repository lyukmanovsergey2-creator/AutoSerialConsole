# -*- coding: utf-8 -*-
"""
AutoSerialConsole (Android) — автоподбор параметров USB-COM и терминал
коммутатора через USB-OTG.

Аналог Windows-версии, портированный на Android (Kivy + usbserial4a).
Подключение: телефон --[OTG USB->Type-C]-- переходник USB-COM (CH340 и пр.) --
консольный кабель -- коммутатор.

ВАЖНО по работе:
- Serial-доступ на Android идёт не через pyserial, а через usbserial4a
  (поверх Android USB Host API). Поддерживаются CH34x, CP210x, FTDI, PL2303.
- При подключении переходника система спрашивает разрешение на доступ к
  USB-устройству — нужно разрешить.

Собирается в APK через buildozer (см. buildozer.spec и .github/workflows).
"""

import time
import threading

from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.utils import platform

import re

# --- Android USB serial (есть только на устройстве) -------------------------
ANDROID = platform == "android"
try:
    from usb4a import usb
    from usbserial4a import serial4a
    USB_OK = True
except Exception as _e:
    usb = None
    serial4a = None
    USB_OK = False
    _USB_ERR = str(_e)


# ============================================================================
#  Логика подбора (та же, что в Windows-версии)
# ============================================================================

AUTO = "Авто"
BAUD_DETECT_ORDER = [9600, 115200, 38400, 57600, 19200, 2400, 4800, 1200, 230400]
GOOD_THRESHOLD = 0.85
MIN_ACCEPT = 0.60

_PRINTABLE = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x08, 0x1B, 0x07}
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")

# вендор -> консольные скорости (приоритет); как в десктоп-версии
VENDOR_DB = {
    "(не задан)": [],
    "D-Link": [9600, 115200],
    "Cisco": [9600],
    "MikroTik": [115200],
    "Eltex": [115200, 9600],
    "3Com/H3C": [38400, 9600, 115200],
    "Huawei": [9600, 38400, 115200],
    "HP/Aruba": [9600],
    "Juniper": [9600],
    "Zyxel": [9600, 115200],
    "TP-Link": [38400, 9600],
    "QTECH": [115200, 9600],
}

VENDOR_PATTERNS = [
    (r"D-?Link|DES-\d|DGS-\d", "D-Link"),
    (r"3Com|Baseline Switch|Comware|H3C|cmw\d", "3Com/H3C"),
    (r"Huawei|VRP|Quidway", "Huawei"),
    (r"Cisco|IOS|Catalyst", "Cisco"),
    (r"MikroTik|RouterOS", "MikroTik"),
    (r"Eltex|MES\d", "Eltex"),
]


def score_readability(data: bytes) -> float:
    if not data:
        return 0.0
    good = sum(1 for b in data if b in _PRINTABLE)
    ratio = good / len(data)
    n = len(data)
    if n < 8:
        return ratio * (n / 8.0)
    return ratio


def detect_vendor(text):
    for pat, name in VENDOR_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return name
    return None


# ============================================================================
#  Приложение
# ============================================================================

class Root(BoxLayout):
    pass


class AutoSerialApp(App):
    def build(self):
        self.title = "AutoSerialConsole"
        self.device = None          # выбранное USB-устройство
        self.device_map = {}        # текст -> android device
        self.port = None            # открытый serial4a порт (терминал)
        self._scan_stop = threading.Event()
        self._reader_stop = threading.Event()
        self._scanning = False

        root = BoxLayout(orientation="vertical", padding=6, spacing=4)

        # строка выбора устройства
        row1 = BoxLayout(size_hint_y=None, height="44dp", spacing=4)
        self.sp_dev = Spinner(text="(нет устройств)", values=[])
        row1.add_widget(self.sp_dev)
        row1.add_widget(Button(text="Обновить", size_hint_x=None, width="110dp",
                               on_release=lambda *_: self.refresh_devices()))
        root.add_widget(row1)

        # строка: вендор + скорость
        row2 = BoxLayout(size_hint_y=None, height="44dp", spacing=4)
        self.sp_vendor = Spinner(text="(не задан)", values=list(VENDOR_DB.keys()))
        row2.add_widget(Label(text="Вендор:", size_hint_x=None, width="70dp"))
        row2.add_widget(self.sp_vendor)
        self.sp_baud = Spinner(text=AUTO, values=[AUTO] + [str(b) for b in BAUD_DETECT_ORDER])
        row2.add_widget(Label(text="Скор.:", size_hint_x=None, width="60dp"))
        row2.add_widget(self.sp_baud)
        root.add_widget(row2)

        # кнопки управления
        row3 = BoxLayout(size_hint_y=None, height="48dp", spacing=4)
        self.btn_start = Button(text="СТАРТ", on_release=lambda *_: self.on_start())
        self.btn_stop = Button(text="Стоп", on_release=lambda *_: self.on_stop(),
                               disabled=True)
        self.btn_disc = Button(text="Отключить", on_release=lambda *_: self.disconnect(),
                               disabled=True)
        row3.add_widget(self.btn_start)
        row3.add_widget(self.btn_stop)
        row3.add_widget(self.btn_disc)
        root.add_widget(row3)

        # статус
        self.lbl_status = Label(text="Готово.", size_hint_y=None, height="26dp",
                                halign="left", valign="middle")
        self.lbl_status.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        root.add_widget(self.lbl_status)

        # терминал (прокручиваемый)
        self.scroll = ScrollView()
        self.txt = Label(text="", size_hint_y=None, markup=False,
                         halign="left", valign="top", font_name="RobotoMono-Regular"
                         if False else "Roboto", color=(0.85, 0.85, 0.85, 1))
        self.txt.bind(width=lambda *_: setattr(self.txt, "text_size",
                                               (self.txt.width, None)))
        self.txt.bind(texture_size=lambda *_: setattr(self.txt, "height",
                                                      self.txt.texture_size[1]))
        self.scroll.add_widget(self.txt)
        root.add_widget(self.scroll)

        # ввод команды + спецкнопки (на мобильном удобнее построчно)
        row4 = BoxLayout(size_hint_y=None, height="48dp", spacing=4)
        self.inp = TextInput(multiline=False, hint_text="команда…",
                             on_text_validate=lambda *_: self.send_line())
        row4.add_widget(self.inp)
        row4.add_widget(Button(text="⏎", size_hint_x=None, width="52dp",
                               on_release=lambda *_: self.send_line()))
        row4.add_widget(Button(text="Tab", size_hint_x=None, width="60dp",
                               on_release=lambda *_: self.send_raw(b"\t")))
        root.add_widget(row4)

        if not USB_OK:
            self._append("ВНИМАНИЕ: модуль usbserial4a недоступен.\n"
                         "Это сборка не для Android или библиотека не включена.\n")
        Clock.schedule_once(lambda *_: self.refresh_devices(), 0.5)
        return root

    # ----------------------------------------------- устройства -------------
    def refresh_devices(self):
        if not USB_OK:
            return
        self.device_map.clear()
        names = []
        try:
            devs = usb.get_usb_device_list()
        except Exception as e:
            self._set_status("Ошибка списка USB: %s" % e)
            devs = []
        for d in devs or []:
            try:
                name = d.getDeviceName()
                pid = d.getProductId()
                vid = d.getVendorId()
                label = "%s (VID:%04x PID:%04x)" % (name, vid, pid)
            except Exception:
                label = str(d)
                name = label
            names.append(label)
            self.device_map[label] = d
        if names:
            self.sp_dev.values = names
            self.sp_dev.text = names[0]
            self._set_status("Найдено USB-устройств: %d" % len(names))
        else:
            self.sp_dev.values = []
            self.sp_dev.text = "(нет устройств)"
            self._set_status("USB-устройства не найдены. Подключите переходник через OTG.")

    def _selected_device(self):
        return self.device_map.get(self.sp_dev.text)

    # ----------------------------------------------- подбор -----------------
    def _build_bauds(self):
        if self.sp_baud.text != AUTO:
            try:
                return [int(self.sp_baud.text)]
            except Exception:
                pass
        bauds = list(BAUD_DETECT_ORDER)
        vb = VENDOR_DB.get(self.sp_vendor.text, [])
        # приоритет скоростей вендора (не исключая остальные)
        for b in reversed(vb):
            if b in bauds:
                bauds.remove(b)
                bauds.insert(0, b)
        return bauds

    def on_start(self):
        if not USB_OK:
            self._set_status("usbserial4a недоступен — это не Android-сборка.")
            return
        dev = self._selected_device()
        if dev is None:
            self._set_status("Выберите USB-устройство.")
            return
        # запрос разрешения на доступ к USB-устройству
        try:
            if not usb.has_usb_permission(dev):
                usb.request_usb_permission(dev)
                self._set_status("Разрешите доступ к USB-устройству и нажмите СТАРТ снова.")
                return
        except Exception as e:
            self._append("Не удалось запросить разрешение USB: %s\n" % e)

        self.device = dev
        self._scan_stop.clear()
        self._scanning = True
        self.btn_start.disabled = True
        self.btn_stop.disabled = False
        self._clear()
        bauds = self._build_bauds()
        self._append("Подбор параметров. Скорости: %s\n"
                     % ", ".join(map(str, bauds)))
        threading.Thread(target=self._scan_worker, args=(dev, bauds), daemon=True).start()

    def on_stop(self):
        self._scan_stop.set()
        self._set_status("Останавливаю подбор…")

    def _open_port(self, dev, baud, bytesize=8, parity="N", stopbits=1, timeout=0.3):
        try:
            name = dev.getDeviceName()
            port = serial4a.get_serial_port(name, baud, bytesize, parity, stopbits,
                                            timeout=timeout)
            if port and port.is_open:
                return port
            if port:
                try:
                    port.open()
                except Exception:
                    pass
                return port
        except Exception as e:
            self._log("открытие %d 8N1 не удалось: %s" % (baud, e))
        return None

    def _scan_worker(self, dev, bauds):
        best = None  # (score, baud, sample, port)
        for baud in bauds:
            if self._scan_stop.is_set():
                break
            self._set_status("Проверка: %d 8N1" % baud)
            port = self._open_port(dev, baud)
            if port is None:
                self._log("%d 8N1  — порт не открылся" % baud)
                continue
            data = self._probe_read(port)
            score = score_readability(data)
            self._log("%d 8N1  — совпадение %d%% (%d байт)"
                      % (baud, int(score * 100), len(data)))
            if best is None or score > best[0]:
                # закрываем прошлого лучшего, если это не он
                if best is not None and best[3] is not None:
                    try:
                        best[3].close()
                    except Exception:
                        pass
                best = (score, baud, data, port)
            else:
                try:
                    port.close()
                except Exception:
                    pass
            if score >= GOOD_THRESHOLD and len(data) >= 3:
                break
            time.sleep(0.1)
        self._finish_scan(best)

    def _probe_read(self, port, probe_time=0.7):
        data = bytearray()
        try:
            try:
                port.reset_input_buffer()
            except Exception:
                pass
            port.write(b"\r\n")
            time.sleep(0.12)
            port.write(b"\r\n")
            end = time.time() + probe_time
            while time.time() < end:
                try:
                    n = port.in_waiting
                except Exception:
                    n = 0
                if n:
                    chunk = port.read(n)
                    if chunk:
                        data += chunk
                        end = time.time() + 0.25
                else:
                    time.sleep(0.03)
        except Exception as e:
            self._log("ошибка чтения: %s" % e)
        return bytes(data)

    @mainthread
    def _finish_scan(self, best):
        self._scanning = False
        self.btn_start.disabled = False
        self.btn_stop.disabled = True
        if not best or best[3] is None:
            self._set_status("Подходящие параметры не определены.")
            return
        score, baud, sample, port = best
        self._append("\nЛучшее совпадение: %d 8N1 (%d%%)\n" % (baud, int(score * 100)))
        if score < MIN_ACCEPT:
            self._append("Уверенность низкая — открываю терминал, попробуйте Enter вручную.\n")
        self._start_terminal(port, baud)

    # ----------------------------------------------- терминал ---------------
    def _start_terminal(self, port, baud):
        self.port = port
        try:
            port.timeout = 0.1
        except Exception:
            pass
        self.btn_disc.disabled = False
        self._append("\n--- Соединение: %d 8N1 ---\n--- Войдите (логин/пароль) ниже ---\n\n"
                     % baud)
        self._reader_stop.clear()
        self._rx_text = ""
        threading.Thread(target=self._reader_worker, daemon=True).start()
        self.send_raw(b"\r\n")

    def _reader_worker(self):
        while not self._reader_stop.is_set():
            try:
                if self.port is None:
                    break
                n = self.port.in_waiting
                data = self.port.read(n if n else 1)
            except Exception:
                break
            if data:
                self._on_rx(data)

    @mainthread
    def _on_rx(self, data):
        text = _ANSI_RE.sub("", data.decode("utf-8", "replace"))
        text = text.replace("\r\n", "\n").replace("\r", "")
        self._append(text)
        # распознавание вендора по баннеру
        try:
            self._rx_text = (getattr(self, "_rx_text", "") + text)[-4000:]
            v = detect_vendor(self._rx_text)
            if v and self.sp_vendor.text == "(не задан)":
                self.sp_vendor.text = v
                self._set_status("Похоже на: %s" % v)
        except Exception:
            pass

    def send_line(self):
        s = self.inp.text
        self.inp.text = ""
        self.send_raw(s.encode("utf-8", "replace") + b"\r")

    def send_raw(self, b: bytes):
        if self.port is None:
            return
        try:
            self.port.write(b)
        except Exception as e:
            self._append("\n[ошибка записи: %s]\n" % e)

    def disconnect(self):
        self._reader_stop.set()
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.btn_disc.disabled = True
        self._append("\n--- Соединение закрыто ---\n")

    # ----------------------------------------------- вывод ------------------
    @mainthread
    def _append(self, text):
        self.txt.text += text
        # автопрокрутка вниз
        Clock.schedule_once(lambda *_: setattr(self.scroll, "scroll_y", 0), 0)

    def _clear(self):
        self.txt.text = ""

    @mainthread
    def _set_status(self, text):
        self.lbl_status.text = text

    def _log(self, text):
        self._append(text + "\n")


if __name__ == "__main__":
    AutoSerialApp().run()
