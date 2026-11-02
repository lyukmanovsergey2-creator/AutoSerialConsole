# -*- coding: utf-8 -*-
"""Smoke-тест main.py без Kivy/Android.

Kivy здесь заменён минимальными заглушками: тест проверяет, что build()
создаёт виджеты, терминальный буфер и открытие порта работают, а не
рендеринг. Настоящий APK всё равно нужно проверять на устройстве.
"""

import json
import os
import shutil
import sys
import tempfile
import types
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)


# --------------------------------------------------------------------------
#  Минимальные заглушки Kivy
# --------------------------------------------------------------------------

class _Widget(object):
    def __init__(self, **kwargs):
        self.children = []
        self.texture_size = (0, 0)
        self.text = kwargs.get("text", "")
        self.width = kwargs.get("width", 100)
        self.height = kwargs.get("height", 100)
        self.size_hint_x = kwargs.get("size_hint_x", 1)
        self.size_hint_y = kwargs.get("size_hint_y", 1)
        self.disabled = kwargs.get("disabled", False)
        self.values = kwargs.get("values", [])
        self.scroll_y = 0
        self._bound = {}
        for key, value in kwargs.items():
            setattr(self, key, value)

    def bind(self, **kwargs):
        self._bound.update(kwargs)

    def setter(self, name):
        return lambda *_: setattr(self, name, 0)

    def add_widget(self, widget):
        self.children.append(widget)

    def remove_widget(self, widget):
        if widget in self.children:
            self.children.remove(widget)


class _Label(_Widget):
    pass


class _Button(_Widget):
    pass


class _TextInput(_Widget):
    pass


class _Spinner(_Widget):
    pass


class _BoxLayout(_Widget):
    pass


class _ScrollView(_Widget):
    pass


class _Clipboard(object):
    text = ""

    @classmethod
    def copy(cls, value):
        cls.text = value


class _App(object):
    def __init__(self):
        self.title = None
        self.root = None

    def run(self):
        pass


class _Clock(object):
    @staticmethod
    def schedule_once(callback, delay=0):
        callback(0)

    @staticmethod
    def schedule_interval(callback, interval=0):
        return None


def _install_kivy_stubs():
    if "kivy" in sys.modules and getattr(sys.modules["kivy"], "_is_stub", False):
        return

    def module(name, **attrs):
        mod = types.ModuleType(name)
        for key, value in attrs.items():
            setattr(mod, key, value)
        sys.modules[name] = mod
        return mod

    kivy = module("kivy", kivy_data_dir=os.path.join(PROJECT_ROOT, "kivy_data"))
    kivy._is_stub = True

    module("kivy.app", App=_App)
    module("kivy.clock", Clock=_Clock, mainthread=lambda f: f)
    module("kivy.core")
    module("kivy.core.clipboard", Clipboard=_Clipboard)
    module("kivy.core.window", Window=module("kivy.core.window.window_stub"))
    module("kivy.uix")
    module("kivy.uix.boxlayout", BoxLayout=_BoxLayout)
    module("kivy.uix.gridlayout", GridLayout=_BoxLayout)
    module("kivy.uix.label", Label=_Label)
    module("kivy.uix.button", Button=_Button)
    module("kivy.uix.textinput", TextInput=_TextInput)
    module("kivy.uix.spinner", Spinner=_Spinner)
    module("kivy.uix.scrollview", ScrollView=_ScrollView)
    module("kivy.utils", platform="win")

    # Window должен быть объектом с атрибутом softinput_mode
    window = types.SimpleNamespace(softinput_mode="")
    sys.modules["kivy.core.window"].Window = window


def _install_usb_stubs():
    """Заглушки usb4a/usbserial4a: без них main.py показывает предупреждение."""
    if "usb4a" in sys.modules:
        return

    usb_mod = types.ModuleType("usb4a")
    usb_mod.usb = types.SimpleNamespace(
        get_usb_device_list=lambda: [],
        get_usb_device=lambda name: None,
        has_usb_permission=lambda dev: True,
        request_usb_permission=lambda dev: None,
    )
    sys.modules["usb4a"] = usb_mod

    usbserial4a = types.ModuleType("usbserial4a")
    serial4a = types.ModuleType("usbserial4a.serial4a")
    serial4a.get_serial_port = lambda *a, **k: None
    usbserial4a.serial4a = serial4a
    sys.modules["usbserial4a"] = usbserial4a
    sys.modules["usbserial4a.serial4a"] = serial4a


_install_kivy_stubs()
_install_usb_stubs()
import main  # noqa: E402


# --------------------------------------------------------------------------
#  Фейковые USB-устройства
# --------------------------------------------------------------------------

class _FakeIface(object):
    def __init__(self, cls):
        self._cls = cls

    def getInterfaceClass(self):
        return self._cls


class _FakeUsbDevice(object):
    def __init__(self, name="/dev/bus/usb/001/002", vid=0x1A86, pid=0x7523,
                 classes=(0xFF,)):
        self._name = name
        self._vid = vid
        self._pid = pid
        self._classes = list(classes)

    def getDeviceName(self):
        return self._name

    def getVendorId(self):
        return self._vid

    def getProductId(self):
        return self._pid

    def getInterfaceCount(self):
        return len(self._classes)

    def getInterface(self, index):
        return _FakeIface(self._classes[index])


class _FakePort(object):
    def __init__(self, opens=True):
        self.is_open = False
        self._opens = opens
        self.USB_READ_TIMEOUT_MILLIS = 5000
        self.write_timeout = None
        self.dtr = False
        self.rts = False
        self.closed = False

    def open(self):
        self.is_open = bool(self._opens)

    def close(self):
        self.is_open = False
        self.closed = True


class AppSmokeTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="asc-test-")
        self.app = main.AutoSerialApp()
        # Не читаем/не пишем настоящий каталог данных пользователя.
        self.app._get_data_dir = lambda: self.tmpdir
        self.root = self.app.build()

    def tearDown(self):
        self.app.disconnect(silent=True)
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_creates_ui(self):
        self.assertIsNotNone(self.root)
        self.assertTrue(self.app._chunk_labels)
        self.assertEqual(self.app._term.text, "")

    def test_softinput_mode(self):
        window = sys.modules["kivy.core.window"].Window
        self.assertEqual(window.softinput_mode, "below_target")

    def test_append_and_ansi(self):
        self.app._append("a\r\nb")
        self.assertEqual(self.app._term.text, "a\nb")

        self.app._rx_chunks.append("\x1b[31mRed\x1b[0m\n")
        self.app._flush_rx()
        self.assertIn("Red", self.app._term.text)

    def test_backspace_from_stream(self):
        self.app._rx_chunks.append("abc\b\bZ")
        self.app._flush_rx()
        self.assertEqual(self.app._term.text, "aZ")

    def test_vendor_detection(self):
        self.app._rx_chunks.append("Cisco IOS Software, C2960\n")
        self.app._flush_rx()
        self.assertEqual(self.app.sp_vendor.text, "Cisco")

    def test_terminal_renders_every_chunk(self):
        # Регрессия: раньше обновлялся только последний Label, поэтому
        # терялась каждая 25-я строка (24, 49, ...).
        for i in range(60):
            self.app._append("line%02d\n" % i)

        rendered = "\n".join(lbl.text for lbl in self.app._chunk_labels)
        self.assertEqual(rendered, self.app._term.text)
        for i in (0, 24, 25, 49, 50, 59):
            self.assertIn("line%02d" % i, rendered)

    def test_terminal_chunks_and_trim(self):
        self.app._append("\n".join("line%d" % i for i in range(120)))
        self.assertGreater(len(self.app._chunk_labels), 1)
        rendered = "\n".join(lbl.text for lbl in self.app._chunk_labels)
        self.assertEqual(rendered, self.app._term.text)

        self.app._append("\n".join("x%d" % i for i in range(2100)))
        self.assertGreater(self.app._rendered_trimmed, 0)
        self.assertLessEqual(len(self.app._chunk_labels), 82)
        rendered = "\n".join(lbl.text for lbl in self.app._chunk_labels)
        self.assertEqual(rendered, self.app._term.text)
        self.assertIn("x2099", rendered)

    def test_copy_terminal_to_clipboard(self):
        self.app._append("line1\nline2")
        self.app.copy_terminal()
        clipboard = sys.modules["kivy.core.clipboard"].Clipboard
        self.assertEqual(clipboard.text, self.app._term.text)
        self.assertIn("Скопировано", self.app.lbl_status.text)

    def test_clear_terminal(self):
        self.app._append("line1\nline2")
        self.app.clear_terminal()
        self.assertEqual(self.app._term.text, "")
        self.assertEqual(len(self.app._chunk_labels), 1)
        self.assertEqual(self.app.lbl_status.text, "Терминал очищен.")

    def test_line_ending_setting(self):
        self.app.port = _FakePort(opens=True)
        self.app.inp.text = "cmd"
        self.app.sp_eol.text = "LF"
        self.app.send_line()
        self.assertEqual(self.app._tx_queue.get_nowait(), b"cmd")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"\n")

        self.app.inp.text = "cmd"
        self.app.sp_eol.text = "CRLF"
        self.app.send_line()
        self.assertEqual(self.app._tx_queue.get_nowait(), b"cmd")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"\r\n")

    def test_terminal_streams_question_mark_before_enter(self):
        self.app.port = _FakePort(opens=True)
        self.app._on_input_text(self.app.inp, "show")
        self.app._on_input_text(self.app.inp, "show ?")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"show")
        self.assertEqual(self.app._tx_queue.get_nowait(), b" ?")
        self.assertTrue(self.app._tx_queue.empty())

    def test_terminal_enter_only_sends_line_ending_after_streaming(self):
        self.app.port = _FakePort(opens=True)
        self.app.sp_eol.text = "CR"
        self.app._on_input_text(self.app.inp, "show ?")
        self.app.inp.text = "show ?"
        self.app.send_line()
        self.assertEqual(self.app._tx_queue.get_nowait(), b"show ?")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"\r")

    def test_terminal_backspace_sends_delete_character(self):
        self.app.port = _FakePort(opens=True)
        self.app._on_input_text(self.app.inp, "showx")
        self.app._on_input_text(self.app.inp, "show")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"showx")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"\x08")

    def test_font_size_changes_labels(self):
        old = self.app._font_size
        self.app.change_font_size(1)
        self.assertEqual(self.app._font_size, old + 1)
        expected = "%dsp" % (old + 1)
        self.assertTrue(all(lbl.font_size == expected
                            for lbl in self.app._chunk_labels))
        self.assertIn("Шрифт:", self.app.lbl_status.text)

    def test_open_port_sets_short_read_timeout(self):
        port = _FakePort(opens=True)
        old_usb, old_serial = main.usb, main.serial4a
        try:
            main.usb = types.SimpleNamespace(has_usb_permission=lambda dev: True)
            main.serial4a = types.SimpleNamespace(
                get_serial_port=lambda *a, **k: port)
            dev = _FakeUsbDevice()
            opened = self.app._open_port(dev, 9600)
        finally:
            main.usb, main.serial4a = old_usb, old_serial

        self.assertIs(opened, port)
        self.assertTrue(port.is_open)
        self.assertEqual(port.USB_READ_TIMEOUT_MILLIS, main.READ_TIMEOUT_MS)
        self.assertEqual(port.write_timeout, main.TX_WRITE_TIMEOUT)

    def test_open_port_rejects_closed_port(self):
        port = _FakePort(opens=False)
        old_usb, old_serial = main.usb, main.serial4a
        try:
            main.usb = types.SimpleNamespace(has_usb_permission=lambda dev: True)
            main.serial4a = types.SimpleNamespace(
                get_serial_port=lambda *a, **k: port)
            opened = self.app._open_port(_FakeUsbDevice(), 9600)
        finally:
            main.usb, main.serial4a = old_usb, old_serial

        self.assertIsNone(opened)
        self.assertTrue(port.closed)

    def test_refresh_devices_shows_serial_only(self):
        serial_dev = _FakeUsbDevice(classes=(0xFF,))
        flash_dev = _FakeUsbDevice(name="/dev/bus/usb/001/003",
                                   vid=0x0781, pid=0x5567, classes=(8,))
        old_usb, old_ok = main.usb, main.USB_OK
        try:
            main.USB_OK = True
            main.usb = types.SimpleNamespace(
                get_usb_device_list=lambda: [serial_dev, flash_dev])
            self.app.refresh_devices()
        finally:
            main.usb, main.USB_OK = old_usb, old_ok

        self.assertEqual(len(self.app.device_map), 1)
        self.assertIn("VID:1a86", self.app.sp_dev.text)
        self.assertIn("скрыто: 1", self.app.lbl_status.text)

    def test_disconnect_without_port(self):
        self.app.disconnect()
        self.assertIsNone(self.app.port)
        self.assertTrue(self.app.btn_disc.disabled)
        self.assertFalse(self.app.btn_start.disabled)

    def test_disconnect_closes_port_and_clears_terminal_input(self):
        port = _FakePort(opens=True)
        self.app.port = port
        self.app.inp.text = "pending"
        self.app._input_sent = "pending"
        self.app.disconnect(silent=True)
        self.assertIsNone(self.app.port)
        self.assertTrue(port.closed)
        self.assertEqual(self.app.inp.text, "")
        self.assertEqual(self.app._input_sent, "")

    # ------------------------------------------------ сохранение лога ------
    def test_save_log_writes_file(self):
        self.app._append("boot ok\nline2")
        self.app.save_log()
        log_dir = os.path.join(self.tmpdir, "logs")
        files = os.listdir(log_dir)
        self.assertEqual(len(files), 1)
        with open(os.path.join(log_dir, files[0]), encoding="utf-8") as f:
            self.assertEqual(f.read(), "boot ok\nline2\n")
        self.assertIn("Лог сохранён", self.app.lbl_status.text)
        self.assertTrue(self.app._last_log_path.endswith(files[0]))

    def test_save_log_refuses_empty_terminal(self):
        self.app.save_log()
        self.assertIn("нечего", self.app.lbl_status.text)
        self.assertFalse(os.path.isdir(os.path.join(self.tmpdir, "logs")))

    # ------------------------------------------------ поиск -----------------
    def test_search_finds_and_highlights(self):
        for i in range(30):
            self.app._append("line%02d error\n" % i)
        self.app.inp_search.text = "ERROR"
        self.app.search_terminal()

        self.assertEqual(len(self.app._matches), 30)
        self.assertIn("30", self.app.lbl_status.text)
        rendered = "\n".join(lbl.text for lbl in self.app._chunk_labels)
        self.assertIn("[color=ffff00]error[/color]", rendered)
        self.assertIn("line00", rendered)

        self.app.next_match(1)
        self.assertEqual(self.app._match_idx, 1)
        self.app.next_match(-1)
        self.assertEqual(self.app._match_idx, 0)

    def test_search_escapes_markup(self):
        self.app._append("value [1] & more\n")
        old = self.app._term.text
        self.app.search_terminal("value")
        text = self.app._chunk_labels[0].text
        self.assertIn("&bl;1&br;", text)
        self.assertIn("&amp;", text)
        self.assertNotIn("[1]", text)

        self.app.clear_search()
        self.assertEqual(self.app._chunk_labels[0].text, old)
        self.assertFalse(self.app._chunk_labels[0].markup)

    def test_search_without_match(self):
        self.app._append("nothing here\n")
        self.app.search_terminal("absent")
        self.assertEqual(self.app._matches, [])
        self.assertIn("Не найдено", self.app.lbl_status.text)
        self.app.next_match(1)
        self.assertIn("Нет совпадений", self.app.lbl_status.text)

    # ------------------------------------------------ макросы ---------------
    def test_macro_add_and_reload(self):
        self.app.inp.text = "show run"
        self.app.add_macro()

        self.assertEqual(self.app.macros, ["show run"])
        with open(os.path.join(self.tmpdir, "macros.json"),
                  encoding="utf-8") as f:
            self.assertEqual(json.load(f), ["show run"])
        self.assertEqual(len(self.app.macro_box.children), 1)

        # Повторное добавление не создаёт дубль.
        self.app.add_macro()
        self.assertEqual(self.app.macros, ["show run"])

        # Загрузка из файла (как при следующем запуске приложения).
        self.app.macros = []
        self.app.load_macros()
        self.assertEqual(self.app.macros, ["show run"])

    def test_macro_add_requires_command(self):
        self.app.inp.text = "   "
        self.app.add_macro()
        self.assertEqual(self.app.macros, [])
        self.assertIn("Введите команду", self.app.lbl_status.text)

    def test_send_macro_uses_line_ending(self):
        self.app.port = _FakePort(opens=True)
        self.app.macros = ["show version"]
        self.app.sp_eol.text = "LF"
        self.app.send_macro("show version")
        self.assertEqual(self.app._tx_queue.get_nowait(), b"show version\n")
        self.assertIn("Отправлено", self.app.lbl_status.text)

    def test_send_macro_without_port(self):
        self.app.macros = ["show version"]
        self.app.send_macro("show version")
        self.assertIn("Порт не открыт", self.app.lbl_status.text)

    def test_macro_edit_mode_removes(self):
        self.app.macros = ["a", "b"]
        self.app.toggle_macro_edit()
        self.assertEqual(self.app.btn_macro_edit.text, "Готово")

        self.app.send_macro("a")
        self.assertEqual(self.app.macros, ["b"])

        self.app.toggle_macro_edit()
        self.assertEqual(self.app.btn_macro_edit.text, "Правка")
        self.assertEqual(self.app._macro_edit, False)


if __name__ == "__main__":
    unittest.main()
