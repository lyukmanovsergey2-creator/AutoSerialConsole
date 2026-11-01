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
  USB-устройству — нужно разрешить (приложение ждёт ответ до 10 секунд).
- usbserial4a читает данные через bulkTransfer с таймаутом 5 с. Без
  переопределения USB_READ_TIMEOUT_MILLIS подбор каждой «молчащей» скорости
  занимал бы ~5 секунд.

Собирается в APK через buildozer (см. buildozer.spec и .github/workflows).
"""

import codecs
import json
import os
import queue
import threading
import time

import kivy
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.textinput import TextInput
from kivy.uix.spinner import Spinner
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window
from kivy.utils import platform

from serial_core import (
    AUTO,
    BAUD_DETECT_ORDER,
    GOOD_THRESHOLD,
    MIN_ACCEPT,
    VENDOR_DB,
    AnsiFilter,
    TerminalModel,
    build_baud_order,
    detect_vendor,
    is_probably_serial,
    score_readability,
)

# --- Android USB serial (есть только на устройстве) -------------------------
ANDROID = platform == "android"
USB_OK = False
_USB_ERR = ""
try:
    from usb4a import usb
    from usbserial4a import serial4a
    USB_OK = True
except Exception as _e:
    usb = None
    serial4a = None
    _USB_ERR = str(_e)


# usbserial4a: столько миллисекунд ждёт USB bulk-чтение (по умолчанию 5000).
READ_TIMEOUT_MS = 250
# Сколько всего ждём ответа на одной скорости при подборе.
PROBE_TIME = 1.2
# Ограничение истории терминала (строк) и размер порции для одного Label.
TERM_MAX_LINES = 2000
TERM_CHUNK_LINES = 25
# Таймаут записи в порт (секунды), чтобы UI не зависал.
TX_WRITE_TIMEOUT = 0.5
# Окончание строки, которое отправляется вместе с командой.
LINE_ENDINGS = {"CR": b"\r", "LF": b"\n", "CRLF": b"\r\n"}
# Файл быстрых команд (макросов) в приватном каталоге приложения.
MACROS_FILE = "macros.json"
MACRO_MAX = 12
MACRO_MAX_LEN = 120


def _escape_markup(text):
    """Экранирует символы разметки Kivy ([], &), чтобы текст выводился как есть."""
    return (text.replace("&", "&amp;")
                .replace("[", "&bl;")
                .replace("]", "&br;"))


def _mono_font_path():
    """Kivy уже содержит моноширинный шрифт; на всякий случай — fallback."""
    try:
        path = os.path.join(kivy.kivy_data_dir, "fonts", "RobotoMono-Regular.ttf")
        if os.path.isfile(path):
            return path
    except Exception:
        pass
    return "Roboto"


# ============================================================================
#  Приложение
# ============================================================================

class AutoSerialApp(App):
    def build(self):
        self.title = "AutoSerialConsole"
        self.device = None          # выбранное USB-устройство
        self.device_map = {}        # текст -> android device
        self.port = None            # открытый serial4a порт (терминал)

        self._scan_stop = threading.Event()
        self._perm_stop = threading.Event()
        self._reader_stop = threading.Event()
        self._writer_stop = threading.Event()
        self._scanning = False
        self._perm_waiting = False

        self._rx_lock = threading.Lock()
        self._rx_chunks = []
        self._rx_text = ""
        self._tx_queue = queue.Queue()

        self._term = TerminalModel(TERM_MAX_LINES, TERM_CHUNK_LINES)
        self._ansi = AnsiFilter()
        self._chunk_labels = []
        self._rendered_trimmed = 0
        self._scroll_pending = False
        self._dtr = True
        self._mono_font = _mono_font_path()
        self._font_size = 13

        # Данные приложения: макросы и сохранённые логи.
        self._data_dir = self._get_data_dir()
        self.macros = []
        self._macro_edit = False
        self._last_log_path = None
        # Поиск по терминалу.
        self._search_query = ""
        self._matches = []
        self._match_idx = 0
        self.load_macros()

        # Клавиатура не должна закрывать поле ввода команды.
        try:
            Window.softinput_mode = "below_target"
        except Exception:
            pass

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
        self.btn_start = Button(text="СТАРТ", on_release=lambda *_: self.start_scan())
        self.btn_stop = Button(text="Стоп", on_release=lambda *_: self.stop_scan(),
                               disabled=True)
        self.btn_disc = Button(text="Отключить", on_release=lambda *_: self.disconnect(),
                               disabled=True)
        self.btn_dtr = Button(text="DTR", on_release=lambda *_: self.toggle_dtr_rts(),
                              disabled=True)
        row3.add_widget(self.btn_start)
        row3.add_widget(self.btn_stop)
        row3.add_widget(self.btn_disc)
        row3.add_widget(self.btn_dtr)
        root.add_widget(row3)

        # статус
        self.lbl_status = Label(text="Готово.", size_hint_y=None, height="30dp",
                                halign="left", valign="middle")
        self.lbl_status.bind(size=lambda w, *_: setattr(w, "text_size", w.size))
        root.add_widget(self.lbl_status)

        # терминал (прокручиваемый, текст разбит на порции Label-ов,
        # чтобы не упереться в лимит размера текстуры GPU)
        self.scroll = ScrollView()
        self.term_box = BoxLayout(orientation="vertical", size_hint_y=None, spacing=0)
        self.term_box.bind(minimum_height=self.term_box.setter("height"))
        self.scroll.add_widget(self.term_box)
        root.add_widget(self.scroll)
        self._sync_terminal_view()

        # инструменты терминала: копирование, очистка, размер шрифта
        row_tools = BoxLayout(size_hint_y=None, height="40dp", spacing=4)
        row_tools.add_widget(Button(text="Копировать", font_size="12sp",
                                    on_release=lambda *_: self.copy_terminal()))
        row_tools.add_widget(Button(text="Очистить", font_size="12sp",
                                    on_release=lambda *_: self.clear_terminal()))
        row_tools.add_widget(Button(text="Лог", size_hint_x=None, width="48dp",
                                    font_size="12sp",
                                    on_release=lambda *_: self.save_log()))
        row_tools.add_widget(Button(text="A-", size_hint_x=None, width="44dp",
                                    font_size="12sp",
                                    on_release=lambda *_: self.change_font_size(-1)))
        row_tools.add_widget(Button(text="A+", size_hint_x=None, width="44dp",
                                    font_size="12sp",
                                    on_release=lambda *_: self.change_font_size(1)))
        root.add_widget(row_tools)

        # окончание строки и спецклавиши
        row_special = BoxLayout(size_hint_y=None, height="40dp", spacing=4)
        row_special.add_widget(Label(text="EOL:", size_hint_x=None, width="44dp",
                                     font_size="12sp"))
        self.sp_eol = Spinner(text="CR", values=["CR", "LF", "CRLF"],
                              size_hint_x=None, width="72dp", font_size="12sp")
        row_special.add_widget(self.sp_eol)
        row_special.add_widget(Button(text="^C", size_hint_x=None, width="48dp",
                                      font_size="12sp",
                                      on_release=lambda *_: self.send_raw(b"\x03")))
        row_special.add_widget(Button(text="Esc", size_hint_x=None, width="48dp",
                                      font_size="12sp",
                                      on_release=lambda *_: self.send_raw(b"\x1b")))
        row_special.add_widget(Button(text="Tab", size_hint_x=None, width="48dp",
                                      font_size="12sp",
                                      on_release=lambda *_: self.send_raw(b"\t")))
        root.add_widget(row_special)

        # быстрые команды (макросы): тап — отправить, «Правка» — удаление
        row_macros = BoxLayout(size_hint_y=None, height="36dp", spacing=4)
        self.macro_scroll = ScrollView(size_hint_x=1, do_scroll_y=False,
                                       do_scroll_x=True, bar_width=2)
        self.macro_box = BoxLayout(orientation="horizontal", size_hint=(None, 1),
                                   spacing=4)
        self.macro_box.bind(minimum_width=self.macro_box.setter("width"))
        self.macro_scroll.add_widget(self.macro_box)
        row_macros.add_widget(self.macro_scroll)
        row_macros.add_widget(Button(text="+", size_hint_x=None, width="40dp",
                                     font_size="14sp",
                                     on_release=lambda *_: self.add_macro()))
        self.btn_macro_edit = Button(text="Правка", size_hint_x=None, width="64dp",
                                     font_size="10sp",
                                     on_release=lambda *_: self.toggle_macro_edit())
        row_macros.add_widget(self.btn_macro_edit)
        root.add_widget(row_macros)

        # поиск по тексту терминала
        row_search = BoxLayout(size_hint_y=None, height="36dp", spacing=4)
        self.inp_search = TextInput(multiline=False, hint_text="поиск…",
                                    font_size="12sp",
                                    on_text_validate=lambda *_: self.search_terminal())
        row_search.add_widget(self.inp_search)
        row_search.add_widget(Button(text="Найти", size_hint_x=None, width="58dp",
                                     font_size="11sp",
                                     on_release=lambda *_: self.search_terminal()))
        row_search.add_widget(Button(text="<", size_hint_x=None, width="32dp",
                                     font_size="12sp",
                                     on_release=lambda *_: self.next_match(-1)))
        row_search.add_widget(Button(text=">", size_hint_x=None, width="32dp",
                                     font_size="12sp",
                                     on_release=lambda *_: self.next_match(1)))
        row_search.add_widget(Button(text="×", size_hint_x=None, width="32dp",
                                     font_size="14sp",
                                     on_release=lambda *_: self.clear_search()))
        root.add_widget(row_search)

        # ввод команды
        row4 = BoxLayout(size_hint_y=None, height="48dp", spacing=4)
        self.inp = TextInput(multiline=False, hint_text="команда…",
                             on_text_validate=lambda *_: self.send_line())
        row4.add_widget(self.inp)
        row4.add_widget(Button(text="⏎", size_hint_x=None, width="52dp",
                               on_release=lambda *_: self.send_line()))
        root.add_widget(row4)

        self._render_macros()

        if not USB_OK:
            self._append("ВНИМАНИЕ: модуль usbserial4a недоступен.\n"
                         "Это сборка не для Android или библиотека не включена.\n")
            if _USB_ERR:
                self._append("Причина: %s\n" % _USB_ERR)

        Clock.schedule_once(lambda *_: self.refresh_devices(), 0.5)
        # Батч-вывод: не перерисовываем Label на каждый USB-чанк.
        Clock.schedule_interval(self._flush_rx, 0.05)
        return root

    # ----------------------------------------------- устройства -------------
    def refresh_devices(self):
        if not USB_OK:
            return
        prev = self.sp_dev.text
        self.device_map.clear()
        names = []
        skipped = 0

        try:
            devs = usb.get_usb_device_list() or []
        except Exception as e:
            self._set_status("Ошибка списка USB: %s" % e)
            devs = []

        serial_devs = [d for d in devs if is_probably_serial(d)]
        skipped = len(devs) - len(serial_devs)
        if not serial_devs and devs:
            # Фильтр оказался слишком строгим — лучше показать всё.
            serial_devs = list(devs)
            skipped = 0

        for d in serial_devs:
            try:
                name = d.getDeviceName()
                pid = d.getProductId()
                vid = d.getVendorId()
                label = "%s (VID:%04x PID:%04x)" % (name, vid, pid)
            except Exception:
                label = str(d)
            names.append(label)
            self.device_map[label] = d

        if names:
            self.sp_dev.values = names
            self.sp_dev.text = prev if prev in names else names[0]
            extra = (" (скрыто: %d)" % skipped) if skipped else ""
            self._set_status("Найдено USB-устройств: %d%s" % (len(names), extra))
        else:
            self.sp_dev.values = []
            self.sp_dev.text = "(нет устройств)"
            self._set_status("USB-устройства не найдены. Подключите переходник через OTG.")

    def _selected_device(self):
        return self.device_map.get(self.sp_dev.text)

    # ----------------------------------------------- подбор -----------------
    def start_scan(self):
        if not USB_OK:
            self._set_status("usbserial4a недоступен — это не Android-сборка.")
            return
        if self._scanning or self._perm_waiting:
            return

        dev = self._selected_device()
        if dev is None:
            self._set_status("Выберите USB-устройство.")
            return

        # Новый подбор при открытом терминале: старый порт обязательно закрыть,
        # иначе Android не даст заново захватить интерфейс.
        if self.port is not None:
            self.disconnect(silent=True)

        try:
            if not usb.has_usb_permission(dev):
                usb.request_usb_permission(dev)
                self._wait_permission(dev)
                return
        except Exception as e:
            self._log("Не удалось проверить разрешение USB: %s" % e)

        self._begin_scan(dev)

    def stop_scan(self):
        if self._perm_waiting:
            self._perm_stop.set()
            self._set_status("Отменяю ожидание разрешения…")
            return
        if not self._scanning:
            return
        self._scan_stop.set()
        self._set_status("Останавливаю подбор…")

    def _wait_permission(self, dev):
        self._perm_stop.clear()
        self._perm_waiting = True
        self.btn_start.disabled = True
        self.btn_stop.disabled = False
        self._set_status("Ожидаю разрешение USB…")
        threading.Thread(target=self._perm_worker, args=(dev,), daemon=True).start()

    def _perm_worker(self, dev):
        granted = False
        for _ in range(20):  # 20 * 0.5 c = 10 c
            if self._perm_stop.is_set():
                break
            try:
                if usb.has_usb_permission(dev):
                    granted = True
                    break
            except Exception:
                pass
            time.sleep(0.5)
        self._after_permission(dev, granted)

    @mainthread
    def _after_permission(self, dev, granted):
        self._perm_waiting = False
        self.btn_start.disabled = False
        self.btn_stop.disabled = True
        if self._perm_stop.is_set():
            self._set_status("Ожидание разрешения отменено.")
            return
        if granted:
            self._begin_scan(dev)
        else:
            self._set_status("Разрешение на USB не выдано. Нажмите СТАРТ и разрешите доступ.")

    def _begin_scan(self, dev):
        self.device = dev
        self._scan_stop.clear()
        self._scanning = True
        self.btn_start.disabled = True
        self.btn_stop.disabled = False
        self.btn_disc.disabled = True
        self.btn_dtr.disabled = True

        self._clear()
        bauds = build_baud_order(self.sp_baud.text, self.sp_vendor.text)
        self._append("Подбор параметров. Скорости: %s\n"
                     % ", ".join(map(str, bauds)))
        threading.Thread(target=self._scan_worker, args=(dev, bauds), daemon=True).start()

    def _open_port(self, dev, baud, bytesize=8, parity="N", stopbits=1, timeout=0.15):
        try:
            if not usb.has_usb_permission(dev):
                self._log("%d 8N1 — нет разрешения USB" % baud)
                return None
        except Exception:
            pass

        try:
            name = dev.getDeviceName()
            port = serial4a.get_serial_port(
                name,
                baudrate=int(baud),
                bytesize=bytesize,
                parity=parity,
                stopbits=stopbits,
                timeout=timeout,
            )
        except Exception as e:
            self._log("открытие %d 8N1 не удалось: %s" % (baud, e))
            return None

        if port is None:
            self._log("открытие %d 8N1: порт не создан" % baud)
            return None

        # usbserial4a открывает порт в конструкторе, но при отсутствии
        # разрешения open() только запрашивает его и оставляет порт закрытым.
        if not port.is_open:
            try:
                port.open()
            except Exception as e:
                self._log("открытие %d 8N1: %s" % (baud, e))

        if not port.is_open:
            try:
                port.close()
            except Exception:
                pass
            self._log("%d 8N1 — порт не открылся (разрешение USB?)" % baud)
            return None

        # Короткий таймаут USB-чтения: иначе in_waiting() блокирует до 5 секунд.
        try:
            port.USB_READ_TIMEOUT_MILLIS = READ_TIMEOUT_MS
        except Exception:
            pass
        try:
            port.write_timeout = TX_WRITE_TIMEOUT
        except Exception:
            pass
        try:
            port.dtr = True
            port.rts = True
        except Exception:
            pass
        return port

    def _probe_read(self, port, probe_time=PROBE_TIME):
        data = bytearray()
        try:
            try:
                port.reset_input_buffer()
            except Exception:
                pass
            port.write(b"\r\n")
            time.sleep(0.1)
            port.write(b"\r\n")

            deadline = time.time() + probe_time
            idle = time.time() + 0.35
            while time.time() < deadline and time.time() < idle:
                try:
                    n = port.in_waiting
                except Exception:
                    break
                if n:
                    chunk = port.read(n)
                    if chunk:
                        data += chunk
                        idle = time.time() + 0.35
                else:
                    time.sleep(0.02)
        except Exception as e:
            self._log("ошибка чтения: %s" % e)
        return bytes(data)

    def _scan_worker(self, dev, bauds):
        best = None  # (score, baud, sample)

        for baud in bauds:
            if self._scan_stop.is_set():
                break
            self._set_status("Проверка: %d 8N1" % baud)

            port = self._open_port(dev, baud)
            if port is None:
                self._log("%d 8N1 — пропуск" % baud)
                continue

            # Порт закрываем сразу: Android обычно не даёт держать открытыми
            # два соединения к одному USB-интерфейсу, а нам нужно проверить
            # все скорости. Лучшую скорость откроем заново в конце.
            try:
                data = self._probe_read(port)
            finally:
                try:
                    port.close()
                except Exception:
                    pass

            score = score_readability(data)
            self._log("%d 8N1 — совпадение %d%% (%d байт)"
                      % (baud, int(score * 100), len(data)))
            if best is None or score > best[0]:
                best = (score, baud, data)

            if score >= GOOD_THRESHOLD and len(data) >= 3:
                break
            if self._scan_stop.wait(0.08):
                break

        aborted = self._scan_stop.is_set()
        self._finish_scan(None if aborted else best, aborted)

    @mainthread
    def _finish_scan(self, best, aborted=False):
        self._scanning = False
        self.btn_start.disabled = False
        self.btn_stop.disabled = True

        if aborted:
            self._set_status("Подбор остановлен.")
            return
        if best is None:
            self._set_status("Параметры не определены — не удалось открыть порт.")
            return

        score, baud, sample = best
        self._append("\nЛучшее совпадение: %d 8N1 (%d%%)\n"
                     % (baud, int(score * 100)))
        if score < MIN_ACCEPT:
            self._append("Уверенность низкая — попробуйте нажать Enter вручную.\n")

        port = self._open_port(self.device, baud)
        if port is None:
            self._set_status("Не удалось открыть порт %d 8N1." % baud)
            return
        self._start_terminal(port, baud)

    # ----------------------------------------------- терминал ---------------
    def _start_terminal(self, port, baud):
        self.port = port
        self._reader_stop.clear()
        self._writer_stop.clear()

        while True:
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                break

        with self._rx_lock:
            self._rx_chunks.clear()
        self._ansi = AnsiFilter()
        self._rx_text = ""
        self._dtr = True
        try:
            port.dtr = True
            port.rts = True
        except Exception:
            pass

        self.btn_disc.disabled = False
        self.btn_dtr.disabled = False
        self.btn_start.disabled = False
        self._append("\n--- Соединение: %d 8N1 ---\n"
                     "--- Войдите (логин/пароль) ниже ---\n\n" % baud)

        threading.Thread(target=self._reader_worker, daemon=True).start()
        threading.Thread(target=self._writer_worker, args=(port,), daemon=True).start()
        self.send_raw(b"\r\n")

    def _reader_worker(self):
        port = self.port
        if port is None:
            return
        decoder = codecs.getincrementaldecoder("utf-8")("replace")

        while not self._reader_stop.is_set() and self.port is port:
            try:
                n = port.in_waiting
                if not n:
                    continue
                data = port.read(n)
            except Exception as e:
                if not self._reader_stop.is_set() and self.port is port:
                    self._on_link_lost(port, str(e))
                break

            if not data:
                continue
            if self.port is not port:
                break
            text = decoder.decode(data)
            if text:
                with self._rx_lock:
                    self._rx_chunks.append(text)

    def _writer_worker(self, port):
        while not self._writer_stop.is_set() and self.port is port:
            try:
                item = self._tx_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if item is None:
                break
            if self.port is not port:
                break
            try:
                port.write(item)
            except Exception as e:
                if not self._writer_stop.is_set() and self.port is port:
                    self._on_tx_error(port, str(e))
                break

    @mainthread
    def _on_link_lost(self, port, err):
        if self.port is not port:
            return
        self._append("\n[связь потеряна: %s]\n" % err)
        self.disconnect(silent=True)
        self._set_status("Связь потеряна. Подключите переходник и нажмите СТАРТ.")

    @mainthread
    def _on_tx_error(self, port, err):
        if self.port is not port:
            return
        self._append("\n[ошибка записи: %s]\n" % err)

    def _flush_rx(self, _dt=0):
        with self._rx_lock:
            if not self._rx_chunks:
                return
            raw = "".join(self._rx_chunks)
            self._rx_chunks.clear()

        text = self._ansi.feed(raw)
        if not text:
            return

        self._term.feed(text)
        self._render_terminal()

        self._rx_text = (self._rx_text + text)[-4000:]
        vendor = detect_vendor(self._rx_text)
        if vendor and self.sp_vendor.text == "(не задан)":
            self.sp_vendor.text = vendor
            self._set_status("Похоже на: %s" % vendor)

    def send_line(self):
        s = self.inp.text
        self.inp.text = ""
        eol = LINE_ENDINGS.get(self.sp_eol.text, b"\r")
        self.send_raw(s.encode("utf-8", "replace") + eol)

    def send_raw(self, b):
        if self.port is None:
            return
        # Не даём очереди расти бесконечно, если адаптер завис.
        if self._tx_queue.qsize() > 256:
            return
        self._tx_queue.put(b)

    def copy_terminal(self):
        text = self._term.text
        if not text:
            self._set_status("Терминал пуст.")
            return
        try:
            # Импорт здесь, чтобы тесты и desktop-запуск не требовали
            # конкретного clipboard-провайдера Kivy.
            from kivy.core.clipboard import Clipboard
            Clipboard.copy(text)
        except Exception as e:
            self._set_status("Не удалось скопировать: %s" % e)
            return
        self._set_status("Скопировано строк: %d" % len(text.splitlines()))

    def clear_terminal(self):
        self._clear()
        self._search_query = ""
        self._matches = []
        self._match_idx = 0
        if hasattr(self, "inp_search"):
            self.inp_search.text = ""
        self._set_status("Терминал очищен.")

    def change_font_size(self, delta):
        size = max(9, min(24, self._font_size + int(delta)))
        if size == self._font_size:
            return
        self._font_size = size
        for lbl in self._chunk_labels:
            lbl.font_size = "%dsp" % size
        self._set_status("Шрифт: %d" % size)

    # ----------------------------------------------- файлы ------------------
    def _get_data_dir(self):
        try:
            data = self.user_data_dir
        except Exception:
            data = ""
        if not data:
            data = os.path.join(os.path.expanduser("~"), ".autoserialconsole")
        return data

    def save_log(self):
        text = self._term.text
        if not text.strip():
            self._set_status("Терминал пуст — сохранять нечего.")
            return
        try:
            log_dir = os.path.join(self._data_dir, "logs")
            os.makedirs(log_dir, exist_ok=True)
            name = time.strftime("terminal-%Y%m%d-%H%M%S.txt")
            path = os.path.join(log_dir, name)
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(text)
                f.write("\n")
        except Exception as e:
            self._set_status("Не удалось сохранить лог: %s" % e)
            return
        self._last_log_path = path
        self._set_status("Лог сохранён: %s (%d стр.)"
                         % (name, len(text.splitlines())))

    # ----------------------------------------------- макросы ----------------
    def _macros_path(self):
        return os.path.join(self._data_dir, MACROS_FILE)

    def load_macros(self):
        try:
            with open(self._macros_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = []
        if not isinstance(data, list):
            data = []
        self.macros = [str(x)[:MACRO_MAX_LEN] for x in data][:MACRO_MAX]

    def save_macros(self):
        try:
            os.makedirs(self._data_dir, exist_ok=True)
            with open(self._macros_path(), "w", encoding="utf-8") as f:
                json.dump(self.macros, f, ensure_ascii=False, indent=1)
        except Exception as e:
            self._set_status("Не удалось сохранить макросы: %s" % e)

    def _render_macros(self):
        box = getattr(self, "macro_box", None)
        if box is None:
            return
        while box.children:
            box.remove_widget(box.children[0])
        for cmd in self.macros:
            if self._macro_edit:
                text = "× " + cmd
                action = (lambda c=cmd: self.remove_macro(c))
            else:
                text = cmd
                action = (lambda c=cmd: self.send_macro(c))
            box.add_widget(Button(
                text=text,
                size_hint=(None, 1),
                width="%ddp" % max(64, min(240, 8 * len(text) + 24)),
                font_size="11sp",
                on_release=lambda *_a, f=action: f()))

    def add_macro(self):
        cmd = self.inp.text.strip()
        if not cmd:
            self._set_status("Введите команду в поле ввода — она станет макросом.")
            return
        if len(cmd) > MACRO_MAX_LEN:
            self._set_status("Слишком длинный макрос (максимум %d)." % MACRO_MAX_LEN)
            return
        if cmd in self.macros:
            self._set_status("Такой макрос уже есть.")
            return
        if len(self.macros) >= MACRO_MAX:
            self._set_status("Лимит макросов: %d." % MACRO_MAX)
            return
        self.macros.append(cmd)
        self.save_macros()
        self._render_macros()
        self._set_status("Макрос добавлен: %s" % cmd)

    def remove_macro(self, cmd):
        if cmd in self.macros:
            self.macros.remove(cmd)
            self.save_macros()
            self._render_macros()
            self._set_status("Макрос удалён: %s" % cmd)

    def toggle_macro_edit(self):
        self._macro_edit = not self._macro_edit
        if hasattr(self, "btn_macro_edit"):
            self.btn_macro_edit.text = "Готово" if self._macro_edit else "Правка"
        self._render_macros()
        if self._macro_edit:
            self._set_status("Режим удаления: нажмите на макрос, чтобы убрать его.")
        else:
            self._set_status("Макросы: нажатие отправляет команду.")

    def send_macro(self, cmd=None):
        if self._macro_edit:
            if cmd is not None:
                self.remove_macro(cmd)
            return
        if not self.macros:
            self._set_status("Макросов пока нет: введите команду и нажмите «+».")
            return
        if cmd is None:
            cmd = self.macros[0]
        if self.port is None:
            self._set_status("Порт не открыт.")
            return
        eol = LINE_ENDINGS.get(self.sp_eol.text, b"\r")
        self.send_raw(cmd.encode("utf-8", "replace") + eol)
        self._set_status("Отправлено: %s" % cmd)

    # ----------------------------------------------- поиск ------------------
    def search_terminal(self, query=None):
        if query is None:
            query = self.inp_search.text
        self._search_query = (query or "").strip().lower()
        self._match_idx = 0
        self._recompute_matches()
        self._render_terminal()
        if not self._search_query:
            self._set_status("Поиск сброшен.")
            return
        if self._matches:
            self._scroll_to_line(self._matches[0])
            self._set_status("Совпадений: %d (1/%d)"
                             % (len(self._matches), len(self._matches)))
        else:
            self._set_status("Не найдено: %s" % self._search_query)

    def next_match(self, delta=1):
        if not self._search_query or not self._matches:
            self._set_status("Нет совпадений.")
            return
        self._match_idx = (self._match_idx + delta) % len(self._matches)
        self._scroll_to_line(self._matches[self._match_idx])
        self._set_status("Совпадений: %d (%d/%d)"
                         % (len(self._matches), self._match_idx + 1,
                            len(self._matches)))

    def clear_search(self):
        self._search_query = ""
        self._matches = []
        self._match_idx = 0
        if hasattr(self, "inp_search"):
            self.inp_search.text = ""
        self._render_terminal()
        self._set_status("Поиск сброшен.")

    def _recompute_matches(self, lines=None):
        if not self._search_query:
            self._matches = []
            self._match_idx = 0
            return
        if lines is None:
            lines = self._term.display_lines()
        q = self._search_query
        self._matches = [i for i, line in enumerate(lines) if q in line.lower()]
        if self._match_idx >= len(self._matches):
            self._match_idx = 0

    def _scroll_to_line(self, line_idx):
        chunk = line_idx // TERM_CHUNK_LINES
        if not (0 <= chunk < len(self._chunk_labels)):
            return
        lbl = self._chunk_labels[chunk]
        scroll_to = getattr(self.scroll, "scroll_to", None)
        if scroll_to is None:
            return
        try:
            scroll_to(lbl, padding=8, animate=False)
        except Exception:
            pass

    def toggle_dtr_rts(self):
        if self.port is None:
            return
        self._dtr = not self._dtr
        try:
            self.port.dtr = self._dtr
            self.port.rts = self._dtr
            self._append("[DTR/RTS %s]\n" % ("ON" if self._dtr else "OFF"))
        except Exception as e:
            self._append("[DTR/RTS: ошибка %s]\n" % e)

    def disconnect(self, silent=False):
        self._reader_stop.set()
        self._writer_stop.set()

        while True:
            try:
                self._tx_queue.get_nowait()
            except queue.Empty:
                break
        self._tx_queue.put(None)  # разбудить писатель

        port = self.port
        self.port = None
        if port is not None:
            try:
                port.close()
            except Exception:
                pass

        self.btn_disc.disabled = True
        self.btn_dtr.disabled = True
        if not self._scanning:
            self.btn_start.disabled = False
        if not silent:
            self._append("\n--- Соединение закрыто ---\n")

    # ----------------------------------------------- вид терминала ----------
    def _new_term_label(self):
        lbl = Label(text="", size_hint_y=None, halign="left", valign="top",
                    markup=False, font_name=self._mono_font,
                    font_size="%dsp" % self._font_size,
                    color=(0.85, 0.85, 0.85, 1))
        lbl.bind(width=lambda w, *_: setattr(w, "text_size", (w.width, None)))
        lbl.bind(texture_size=lambda w, *_: setattr(w, "height",
                                                    max(w.texture_size[1], 1)))
        return lbl

    def _render_terminal(self):
        self._sync_terminal_view()
        # При активном поиске не уводим экран вниз: пользователь смотрит
        # на найденные совпадения, а не на последнюю строку.
        if self._search_query:
            return
        if not self._scroll_pending:
            self._scroll_pending = True
            Clock.schedule_once(self._scroll_bottom, 0)

    def _markup_line(self, line):
        """Подсвечивает все вхождения запроса жёлтым, экранируя разметку."""
        q = self._search_query
        if not q:
            return line
        low = line.lower()
        out = []
        pos = 0
        while True:
            found = low.find(q, pos)
            if found < 0:
                break
            out.append(_escape_markup(line[pos:found]))
            out.append("[color=ffff00]%s[/color]"
                       % _escape_markup(line[found:found + len(q)]))
            pos = found + len(q)
        out.append(_escape_markup(line[pos:]))
        return "".join(out)

    def _scroll_bottom(self, *_):
        self._scroll_pending = False
        self.scroll.scroll_y = 0

    def _sync_terminal_view(self):
        # Держим ровно столько Label, сколько нужно для текущего числа строк.
        # При обрезке истории модель уменьшает число строк — лишние Label
        # убираем с начала, потому что там самые старые строки.
        lines = self._term.display_lines()
        need = max(1, (len(lines) + TERM_CHUNK_LINES - 1) // TERM_CHUNK_LINES)
        # Совпадения пересчитываем на каждом обновлении: поток вывода может
        # добавить новые строки или обрезать историю.
        self._recompute_matches(lines)
        markup = bool(self._search_query)

        while len(self._chunk_labels) > need:
            self.term_box.remove_widget(self._chunk_labels.pop(0))
        while len(self._chunk_labels) < need:
            lbl = self._new_term_label()
            self._chunk_labels.append(lbl)
            self.term_box.add_widget(lbl)

        # Обновляем ВСЕ чанки, а не только последний: иначе при выводе
        # нескольких строк за один раз (баннер, show run) промежуточные
        # Label-ы остаются пустыми, а на границе чанка теряется строка.
        for idx, lbl in enumerate(self._chunk_labels):
            start = idx * TERM_CHUNK_LINES
            chunk = lines[start:start + TERM_CHUNK_LINES]
            if markup:
                text = "\n".join(self._markup_line(line) for line in chunk)
            else:
                text = "\n".join(chunk)
            lbl.markup = markup
            if lbl.text != text:
                lbl.text = text

        self._rendered_trimmed = self._term.trimmed

    def _clear(self):
        self._term = TerminalModel(TERM_MAX_LINES, TERM_CHUNK_LINES)
        self._ansi = AnsiFilter()
        self._rx_text = ""
        with self._rx_lock:
            self._rx_chunks.clear()

        for lbl in self._chunk_labels:
            self.term_box.remove_widget(lbl)
        self._chunk_labels = []
        self._rendered_trimmed = 0
        self._sync_terminal_view()

    # ----------------------------------------------- вывод ------------------
    @mainthread
    def _append(self, text):
        if not text:
            return
        self._term.feed(text)
        self._render_terminal()

    @mainthread
    def _set_status(self, text):
        self.lbl_status.text = text

    def _log(self, text):
        self._append(text + "\n")

    # ----------------------------------------------- жизненный цикл ---------
    def on_stop(self):
        # Это событие Kivy (закрытие приложения), а не кнопка «Стоп».
        self._perm_stop.set()
        self._scan_stop.set()
        self.disconnect(silent=True)

    def on_pause(self):
        return True

    def on_resume(self):
        self.refresh_devices()
        if self.port is None or self.device is None:
            return
        try:
            names = {d.getDeviceName() for d in (usb.get_usb_device_list() or [])}
            name = self.device.getDeviceName()
        except Exception:
            return
        if name not in names:
            self._append("\n[USB-устройство отключено]\n")
            self.disconnect(silent=True)
            self._set_status("USB-устройство отключено. Подключите переходник и нажмите СТАРТ.")


if __name__ == "__main__":
    AutoSerialApp().run()
