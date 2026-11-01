# -*- coding: utf-8 -*-
"""
Чистая логика AutoSerialConsole.

Здесь нет ни Kivy, ни Android-зависимостей — модуль можно импортировать и
тестировать на обычном компьютере:

    python -m unittest discover -s tests -v
"""

import re

AUTO = "Авто"
BAUD_DETECT_ORDER = [9600, 115200, 38400, 57600, 19200, 2400, 4800, 1200, 230400]
GOOD_THRESHOLD = 0.85
MIN_ACCEPT = 0.60
MIN_SAMPLE_BYTES = 12

_PRINTABLE = set(range(0x20, 0x7F)) | {0x09, 0x0A, 0x0D, 0x08, 0x1B}

# вендор -> консольные скорости (приоритет)
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

# Шаблоны намеренно с \b: иначе "IOS" срабатывает на "BIOS"/"kiosk",
# а "VRP" — на случайный текст в баннере.
VENDOR_PATTERNS = [
    (r"\bD-?Link\b|DES-\d|DGS-\d", "D-Link"),
    (r"\b3Com\b|Baseline Switch|Comware|\bH3C\b|\bcmw\d", "3Com/H3C"),
    (r"\bHuawei\b|\bVRP\b|Quidway", "Huawei"),
    (r"\bCisco\b|\bIOS\b|Catalyst", "Cisco"),
    (r"\bMikroTik\b|RouterOS", "MikroTik"),
    (r"\bEltex\b|MES\d", "Eltex"),
    (r"\bAruba\b|\bHP\b|ProCurve", "HP/Aruba"),
    (r"\bJuniper\b|Junos", "Juniper"),
    (r"\bZyxel\b", "Zyxel"),
    (r"\bTP-?Link\b", "TP-Link"),
    (r"\bQtech\b", "QTECH"),
]

# Классы интерфейсов USB, которые заведомо не являются последовательным портом
# (аудио, HID, still image, принтер, mass storage, хаб, видео).
_EXCLUDED_IFACE_CLASSES = frozenset({1, 3, 6, 7, 8, 9, 14})


def score_readability(data):
    """Доля «читаемых» байт. Используется для автоподбора скорости."""
    if not data:
        return 0.0
    good = sum(1 for b in data if b in _PRINTABLE)
    ratio = good / len(data)
    n = len(data)
    if n < MIN_SAMPLE_BYTES:
        return ratio * (n / float(MIN_SAMPLE_BYTES))
    return ratio


def detect_vendor(text):
    """Вернуть название вендора по баннеру или None."""
    if not text:
        return None
    for pat, name in VENDOR_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            return name
    return None


def build_baud_order(selected_baud, vendor):
    """Список скоростей для проверки: явная скорость либо AUTO + приоритет вендора."""
    if selected_baud != AUTO:
        try:
            return [int(selected_baud)]
        except (TypeError, ValueError):
            pass
    bauds = list(BAUD_DETECT_ORDER)
    for b in reversed(VENDOR_DB.get(vendor, [])):
        if b in bauds:
            bauds.remove(b)
            bauds.insert(0, b)
    return bauds


def is_probably_serial(usb_device):
    """Отсеять явно не-serial USB-устройства (флешки, клавиатуры, хабы).

    При любой ошибке устройство НЕ скрывается: лучше показать лишнее,
    чем потерять рабочий переходник.
    """
    try:
        count = usb_device.getInterfaceCount()
        if not count:
            return True
        for i in range(count):
            if usb_device.getInterface(i).getInterfaceClass() not in _EXCLUDED_IFACE_CLASSES:
                return True
        return False
    except Exception:
        return True


# ============================================================================
#  ANSI / управляющие последовательности
# ============================================================================

_ANSI_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[ -/]*[@-~]"           # CSI: ESC [ ... финальный байт
    r"|\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC: ESC ] ... BEL или ESC \
    r"|[()][0-9A-Za-z]"                # designation: ESC ( B и т.п.
    r"|[@-Z\\-_]"                      # двухсимвольные ESC-последовательности
    r")"
)

# Хвост, который может оказаться началом разрезанной последовательности.
_ANSI_INCOMPLETE_RE = re.compile(
    r"\x1b(?:"
    r"\[[0-9;?]*[ -/]*"
    r"|\][^\x07\x1b]*(?:\x1b)?"
    r"|[()]"
    r")?$"
)

# Управляющие символы, которые не показываем. Backspace (\x08) и DEL (\x7f)
# сохраняем: их обрабатывает TerminalModel (удаление символа).
_CONTROL_RE = re.compile(r"[\x00-\x07\x0b\x0c\x0e-\x1f]")


class AnsiFilter(object):
    """Удаляет ANSI-последовательности, корректно склеивая разрезанные чанки."""

    MAX_PENDING = 256

    def __init__(self):
        self._pending = ""

    def reset(self):
        self._pending = ""

    def feed(self, text):
        if not text:
            return ""
        text = self._pending + text
        self._pending = ""

        m = _ANSI_INCOMPLETE_RE.search(text)
        if m is not None:
            self._pending = text[m.start():]
            if len(self._pending) > self.MAX_PENDING:
                # Битый/незавершённый поток: не держим его вечно.
                self._pending = ""
            text = text[:m.start()]

        text = _ANSI_RE.sub("", text)
        return _CONTROL_RE.sub("", text)


# ============================================================================
#  Модель терминала (перевод строки, CR, backspace, табы, ограничение буфера)
# ============================================================================

TAB_SIZE = 4
MAX_LINE_CHARS = 4000


class TerminalModel(object):
    """Хранит текст терминала, корректно обрабатывая \r, \b и \t.

    completed — завершённые строки, current — текущая строка (список символов).
    trimmed — сколько завершённых строк удалено с начала; нужно представлению,
    чтобы вовремя убирать старые Label-ы.
    """

    def __init__(self, max_lines=2000, trim_step=25):
        self.max_lines = max(1, int(max_lines))
        self.trim_step = max(1, int(trim_step))
        self.completed = []
        self.current = []
        self.cursor = 0
        self.trimmed = 0

    def clear(self):
        self.completed = []
        self.current = []
        self.cursor = 0
        self.trimmed = 0

    def display_lines(self):
        """Строки для отображения (последняя — текущая, может быть пустой)."""
        return self.completed + ["".join(self.current)]

    @property
    def text(self):
        return "\n".join(self.display_lines())

    def feed(self, text):
        if not text:
            return

        completed = self.completed
        current = self.current
        cursor = self.cursor

        for ch in text:
            if ch == "\n":
                completed.append("".join(current))
                current.clear()
                cursor = 0
            elif ch == "\r":
                cursor = 0
            elif ch == "\b" or ch == "\x7f":
                if cursor > 0:
                    del current[cursor - 1]
                    cursor -= 1
            elif ch == "\t":
                spaces = TAB_SIZE - (cursor % TAB_SIZE)
                for _ in range(spaces):
                    if cursor < len(current):
                        current[cursor] = " "
                    else:
                        current.append(" ")
                    cursor += 1
            elif ch < " ":
                # прочие управляющие символы не показываем
                continue
            else:
                if cursor < len(current):
                    current[cursor] = ch
                else:
                    current.append(ch)
                cursor += 1

        self.cursor = cursor
        self._trim()

    def _trim(self):
        if len(self.completed) > self.max_lines:
            excess = len(self.completed) - self.max_lines
            drop = ((excess + self.trim_step - 1) // self.trim_step) * self.trim_step
            drop = min(drop, len(self.completed))
            if drop:
                del self.completed[:drop]
                self.trimmed += drop

        # Защита от одной бесконечной строки (например, битого потока).
        if len(self.current) > MAX_LINE_CHARS:
            drop = len(self.current) - MAX_LINE_CHARS
            del self.current[:drop]
            self.cursor = max(0, self.cursor - drop)
