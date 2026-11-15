"""Exercise USB setup with real Kivy widgets and input dispatch.

Run separately from unittest discovery, for example under Linux Xvfb:
    xvfb-run -a python tests/ui_usb_smoke.py

Only the Android USB modules are replaced. This catches widget construction,
layout and input-routing failures hidden by the lightweight unit-test stubs.
"""

import os
from pathlib import Path
import sys
import tempfile
import time
import types
from unittest.mock import patch


os.environ.setdefault("KIVY_NO_ARGS", "1")
os.environ.setdefault("KIVY_NO_CONSOLELOG", "1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class UsbAdapter:
    def getDeviceName(self):
        return "/dev/bus/usb/001/002"

    def getVendorId(self):
        return 0x1A86

    def getProductId(self):
        return 0x7523

    def getInterfaceCount(self):
        return 1

    def getInterface(self, index):
        assert index == 0
        return types.SimpleNamespace(getInterfaceClass=lambda: 0xFF)


class SerialPort:
    is_open = True
    in_waiting = 0

    def __init__(self):
        self.written = []

    def write(self, data):
        self.written.append(data)
        return len(data)

    def close(self):
        self.is_open = False


adapter = UsbAdapter()
devices = [adapter]
usb_module = types.ModuleType("usb4a")
usb_module.usb = types.SimpleNamespace(
    get_usb_device_list=lambda: list(devices),
    has_usb_permission=lambda device: device is adapter,
    request_usb_permission=lambda device: None,
)
serial_module = types.ModuleType("usbserial4a")
serial_module.serial4a = types.SimpleNamespace(
    get_serial_port=lambda *args, **kwargs: SerialPort(),
)
sys.modules["usb4a"] = usb_module
sys.modules["usbserial4a"] = serial_module

# These imports intentionally use the installed Kivy implementation.
from kivy.base import EventLoop  # noqa: E402
from kivy.core.window import Window  # noqa: E402
from kivy.input.motionevent import MotionEvent  # noqa: E402
from kivy.metrics import dp  # noqa: E402
from kivy.uix.button import Button  # noqa: E402
import main  # noqa: E402


class Touch(MotionEvent):
    """Feed a real touch through the window, including ScrollView handling."""

    def __init__(self, x, y):
        super().__init__(
            "ui-usb-smoke", 1,
            {"x": x / (Window.width - 1), "y": y / (Window.height - 1)},
            is_touch=True, type_id="touch",
        )
        self.profile = ["pos"]

    def depack(self, args):
        self.sx = args["x"]
        self.sy = args["y"]
        super().depack(args)


def advance(seconds=0.1):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        EventLoop.idle()


def tap(widget):
    assert not widget.disabled, f"Disabled control: {widget.text!r}"
    assert widget.width >= dp(44), f"Control too narrow: {widget.text!r}"
    assert widget.height >= dp(44), f"Control too short: {widget.text!r}"
    x, y = widget.to_window(*widget.center)
    assert 0 < x < Window.width and 0 < y < Window.height, (
        f"Control outside window: {widget.text!r} at {(x, y)!r}"
    )
    touch = Touch(x, y)
    EventLoop.post_dispatch_input("begin", touch)
    advance()
    EventLoop.post_dispatch_input("end", touch)
    advance()


def run():
    Window.size = (390, 844)
    with tempfile.TemporaryDirectory(prefix="asc-real-ui-") as data_dir:
        app = main.AutoSerialApp()
        app._get_data_dir = lambda: data_dir
        try:
            app._run_prepare()
            advance(0.65)
            assert app.root is app.screens
            assert app.screens.current == "connection"
            assert app._selected_device() is adapter
            assert "VID:1a86" in app.sp_dev.text
            assert not app.btn_start.disabled

            connection = app.screens.get_screen("connection")
            refresh = next(widget for widget in connection.walk()
                           if isinstance(widget, Button) and widget.text == "Обновить")
            devices.clear()
            tap(refresh)
            assert app._selected_device() is None
            assert not app.btn_start.disabled
            devices.append(adapter)
            tap(refresh)
            assert app._selected_device() is adapter

            with patch.object(app, "_begin_scan") as begin_scan:
                tap(app.btn_start)
                begin_scan.assert_called_once_with(adapter)

            # Complete the public scan callback to open the real terminal UI.
            app.device = adapter
            app._finish_scan((1.0, 9600, b"switch>"))
            advance()
            assert app.screens.current == "terminal"
            assert app.port is not None and app.port.is_open
            assert not app._command_row.disabled
            assert "9600" in app.lbl_link.text
            assert app.scroll.height >= Window.height * 0.6
            assert app._tool_slot.height == 0

            # Detach must preserve the log screen and prevent further input.
            port = app.port
            devices.clear()
            app._poll_usb()
            advance()
            assert app.screens.current == "terminal"
            assert app.port is None and not port.is_open
            assert app._command_row.disabled
            assert app._term.text

            back = next(widget for widget in app.screens.get_screen("terminal").walk()
                        if isinstance(widget, Button) and widget.text == "‹ USB")
            tap(back)
            assert app.screens.current == "connection"
            assert not app.btn_start.disabled
            tap(app.btn_last_session)
            assert app.screens.current == "terminal"
            assert app._command_row.disabled
            print("Real Kivy USB smoke passed: discovery, touch input, scan entry, "
                  "terminal layout, detach and session review")
        finally:
            app.stop()


if __name__ == "__main__":
    run()
