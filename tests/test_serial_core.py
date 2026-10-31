# -*- coding: utf-8 -*-
"""Тесты чистой логики (запускаются без Kivy и Android)."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from serial_core import (  # noqa: E402
    AUTO,
    AnsiFilter,
    TerminalModel,
    build_baud_order,
    detect_vendor,
    is_probably_serial,
    score_readability,
)


class ScoreReadabilityTests(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(score_readability(b""), 0.0)

    def test_plain_text(self):
        self.assertEqual(score_readability(b"Hello, switch!"), 1.0)

    def test_binary(self):
        self.assertEqual(score_readability(b"\x00\x01\x02\x03\x04\x05\x06\x07"), 0.0)

    def test_short_sample_is_penalized(self):
        # 4 печатных байта — мало для уверенного совпадения
        self.assertAlmostEqual(score_readability(b"ABCD"), 0.5)


class DetectVendorTests(unittest.TestCase):
    def test_cisco(self):
        self.assertEqual(detect_vendor("Cisco IOS Software, C2960"), "Cisco")

    def test_ios_word_boundary(self):
        self.assertIsNone(detect_vendor("BIOS version 2.1"))
        self.assertIsNone(detect_vendor("welcome to the kiosk"))

    def test_huawei(self):
        self.assertEqual(
            detect_vendor("Huawei Versatile Routing Platform Software VRP V8"),
            "Huawei",
        )

    def test_mikrotik(self):
        self.assertEqual(detect_vendor("MikroTik RouterOS 7.1"), "MikroTik")

    def test_other_vendors(self):
        cases = [
            ("ArubaOS ProCurve", "HP/Aruba"),
            ("ZyXEL ES-2024", "Zyxel"),
            ("TP-Link JetStream", "TP-Link"),
            ("QTECH QSW-3400", "QTECH"),
            ("D-Link DES-3200", "D-Link"),
            ("3Com Baseline Switch", "3Com/H3C"),
            ("H3C Comware Software", "3Com/H3C"),
            ("Juniper Junos 21.4", "Juniper"),
            ("Eltex MES2424", "Eltex"),
        ]
        for text, expected in cases:
            self.assertEqual(detect_vendor(text), expected, text)

    def test_unknown(self):
        self.assertIsNone(detect_vendor("some random boot output"))
        self.assertIsNone(detect_vendor(""))


class BuildBaudOrderTests(unittest.TestCase):
    def test_explicit_baud(self):
        self.assertEqual(build_baud_order("9600", "Cisco"), [9600])

    def test_vendor_priority(self):
        order = build_baud_order(AUTO, "MikroTik")
        self.assertEqual(order[0], 115200)
        self.assertIn(9600, order)

    def test_no_duplicates(self):
        order = build_baud_order(AUTO, "D-Link")
        self.assertEqual(len(order), len(set(order)))

    def test_invalid_baud_falls_back(self):
        order = build_baud_order("not-a-number", "Cisco")
        self.assertEqual(order[0], 9600)

    def test_unknown_vendor(self):
        order = build_baud_order(AUTO, "(не задан)")
        self.assertEqual(order[0], 9600)


class _FakeInterface(object):
    def __init__(self, iface_class):
        self._class = iface_class

    def getInterfaceClass(self):
        return self._class


class _FakeDevice(object):
    def __init__(self, classes):
        self._classes = classes

    def getInterfaceCount(self):
        return len(self._classes)

    def getInterface(self, index):
        return _FakeInterface(self._classes[index])


class _BrokenDevice(object):
    def getInterfaceCount(self):
        raise RuntimeError("boom")


class IsProbablySerialTests(unittest.TestCase):
    def test_hid_is_filtered(self):
        self.assertFalse(is_probably_serial(_FakeDevice([3])))

    def test_mass_storage_is_filtered(self):
        self.assertFalse(is_probably_serial(_FakeDevice([8])))

    def test_hub_is_filtered(self):
        self.assertFalse(is_probably_serial(_FakeDevice([9])))

    def test_vendor_specific_serial_is_kept(self):
        self.assertTrue(is_probably_serial(_FakeDevice([0xFF])))

    def test_cdc_is_kept(self):
        self.assertTrue(is_probably_serial(_FakeDevice([2, 10])))

    def test_composite_with_serial_is_kept(self):
        self.assertTrue(is_probably_serial(_FakeDevice([3, 0xFF])))

    def test_errors_do_not_hide_device(self):
        self.assertTrue(is_probably_serial(_BrokenDevice()))


class AnsiFilterTests(unittest.TestCase):
    def test_csi(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("\x1b[31mRed\x1b[0m"), "Red")

    def test_osc(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("\x1b]0;title\x07Hi"), "Hi")

    def test_split_csi(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("abc\x1b[3"), "abc")
        self.assertEqual(f.feed("1mX"), "X")

    def test_split_osc(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("a\x1b]0;ti"), "a")
        self.assertEqual(f.feed("tle\x07b"), "b")

    def test_charset_designation(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("\x1b(Bx"), "x")

    def test_keeps_newline_and_tab(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("a\x00b\tc\r\nd"), "ab\tc\r\nd")

    def test_keeps_backspace_and_del(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("ab\b\x7f"), "ab\b\x7f")

    def test_stray_escape_is_dropped(self):
        f = AnsiFilter()
        self.assertEqual(f.feed("a\x1b"), "a")
        self.assertEqual(f.feed("b"), "b")


class TerminalModelTests(unittest.TestCase):
    def test_newline(self):
        m = TerminalModel()
        m.feed("a\nb")
        self.assertEqual(m.text, "a\nb")

    def test_carriage_return_overwrites(self):
        m = TerminalModel()
        m.feed("abc\rXY")
        self.assertEqual(m.text, "XYc")

    def test_backspace(self):
        m = TerminalModel()
        m.feed("abc\b\bZ")
        self.assertEqual(m.text, "aZ")

    def test_tab(self):
        m = TerminalModel()
        m.feed("a\tb")
        self.assertEqual(m.text, "a   b")

    def test_control_chars_ignored(self):
        m = TerminalModel()
        m.feed("a\x00b\x1bc")
        self.assertEqual(m.text, "abc")

    def test_trim_keeps_tail_and_counts(self):
        m = TerminalModel(max_lines=5, trim_step=2)
        m.feed("1\n2\n3\n4\n5\n6\n7\n8\n")
        self.assertEqual(m.trimmed, 4)
        self.assertEqual(m.text, "5\n6\n7\n8\n")

    def test_long_line_is_capped(self):
        m = TerminalModel()
        m.feed("x" * 4100)
        self.assertEqual(len(m.current), 4000)
        self.assertEqual(m.cursor, 4000)

    def test_clear(self):
        m = TerminalModel()
        m.feed("abc\n")
        m.clear()
        self.assertEqual(m.text, "")


if __name__ == "__main__":
    unittest.main()
