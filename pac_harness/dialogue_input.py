"""Small Windows console reader for ToUser's immediate Ctrl+G return key.

Only the default interactive console input uses this reader. Redirected input
retains Python's normal ``input`` semantics, and callers can still inject their
own input function. No console modes are changed.
"""
from __future__ import annotations

import builtins
from collections import deque
import os
import sys
import time
import unicodedata


RETURN_TO_TASK = "\x07"
_pending_chars: deque[str] = deque()


def _windows_backend():
    """Return a wide-character console backend, or None without reading input."""
    if os.name != "nt":
        return None
    try:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            return None
        import ctypes
        from ctypes import wintypes
        import msvcrt

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        get_console_mode = kernel32.GetConsoleMode
        get_console_mode.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        get_console_mode.restype = wintypes.BOOL
        mode = wintypes.DWORD()
        if not get_console_mode(msvcrt.get_osfhandle(sys.stdin.fileno()), ctypes.byref(mode)):
            return None
        return msvcrt
    except (AttributeError, OSError, ValueError):
        return None


def _cell_width(character: str) -> int:
    if unicodedata.combining(character) or unicodedata.category(character) in {"Cf", "Cc"}:
        return 0
    return 2 if unicodedata.east_asian_width(character) in {"W", "F"} else 1


def _read_line(prompt, read_char, write):
    """Read append/backspace input; dependencies are injectable for offline tests.

    Windows wide input may deliver astral Unicode as two UTF-16 code units.
    Combine them before echoing, so valid emoji never reach stdout as lone
    surrogates. Arrow/function key pairs are ignored instead of entering text.
    """
    write(prompt)
    characters = []
    high_surrogate = None
    deferred = None
    while True:
        character = deferred if deferred is not None else read_char()
        deferred = None
        if not character:
            write("\n")
            raise EOFError
        if high_surrogate is not None:
            if 0xDC00 <= ord(character) <= 0xDFFF:
                character = chr(0x10000 + ((ord(high_surrogate) - 0xD800) << 10)
                                + ord(character) - 0xDC00)
            else:
                deferred = character
                character = "\ufffd"
            high_surrogate = None
        elif 0xD800 <= ord(character) <= 0xDBFF:
            high_surrogate = character
            continue
        elif 0xDC00 <= ord(character) <= 0xDFFF:
            character = "\ufffd"
        if character == RETURN_TO_TASK:
            write("\n")
            return RETURN_TO_TASK
        if character == "\x03":
            write("\n")
            raise KeyboardInterrupt
        if character == "\x1a":
            write("\n")
            raise EOFError
        if character in {"\r", "\n"}:
            write("\n")
            return "".join(characters)
        if character in {"\x00", "\xe0"}:
            # msvcrt emits a prefix followed by a scan code for extended keys.
            read_char()
            continue
        if character in {"\b", "\x7f"}:
            width = 0
            if characters:
                removed = characters.pop()
                width += _cell_width(removed)
                while characters and unicodedata.combining(removed):
                    removed = characters.pop()
                    width += _cell_width(removed)
            write("\b \b" * width)
            continue
        if character == "\t":
            # Fixed spaces keep backspace echo deterministic without cursor APIs.
            character = "    "
        elif not character.isprintable() and not unicodedata.combining(character):
            continue
        characters.extend(character)
        write(character)


def _write_console(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def read_dialogue_input(prompt: str) -> str:
    """Like input(), with immediate Ctrl+G (``'\\x07'``) on Windows consoles."""
    backend = _windows_backend()
    if backend is None:
        return builtins.input(prompt)

    def read_char():
        if _pending_chars:
            return _pending_chars.popleft()
        # Avoid blocking inside getwch: Python must get a chance to deliver
        # Ctrl+C even when Windows consumes it as a console control event.
        while not backend.kbhit():
            time.sleep(0.02)
        return backend.getwch()

    return _read_line(prompt, read_char, _write_console)


def drain_entry_hotkey():
    """Discard already queued Ctrl+G only, preserving queued user text.

    Call ONCE when opening the dialogue, after pausing ConsoleHotkey. Its global
    listener observes key state without consuming stdin, so the launch key can
    remain in the console input queue. Never call before every dialogue prompt:
    a later queued Ctrl+G is an intentional request to return to the task.
    Noninteractive/custom-input callers should not call this helper.
    """
    backend = _windows_backend()
    if backend is None:
        return
    kept = deque(character for character in _pending_chars if character != RETURN_TO_TASK)
    _pending_chars.clear()
    _pending_chars.extend(kept)
    # Bound a snapshot drain so continuously arriving pasted input cannot delay
    # the first prompt. Unconsumed input stays in the native console queue.
    for _ in range(8192):
        if not backend.kbhit():
            break
        character = backend.getwch()
        if character != RETURN_TO_TASK:
            _pending_chars.append(character)
