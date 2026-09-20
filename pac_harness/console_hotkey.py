"""Queue Ctrl+G dialogue requests without reading stdin or handling Ctrl+C.

The listener only sets a flag. The control loop decides when a bounded action
and its inspection have finished and it is appropriate to open the dialogue.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import sys
import threading


class _WindowsKeyState:
    """Read key state for the launch console window, without consuming input.

    Windows Terminal and VS Code use an invisible pseudoconsole HWND. For those
    hosts we capture their foreground window when the listener starts. Win32
    does not distinguish individual tabs/panes inside that one host window.
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetConsoleWindow.argtypes = []
        kernel32.GetConsoleWindow.restype = wintypes.HWND
        self.user32.GetForegroundWindow.argtypes = []
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        console_window = kernel32.GetConsoleWindow()
        if not console_window:
            raise OSError("no Windows console is attached")
        if self.user32.IsWindowVisible(console_window):
            self.window = console_window
        else:
            # 伪控制台的窗口本身不可见；限定在启动时的终端宿主窗口内。
            self.window = self.user32.GetForegroundWindow()
        if not self.window:
            raise OSError("no foreground terminal window is available")

    def __call__(self):
        if self.user32.GetForegroundWindow() != self.window:
            return False
        return bool(self.user32.GetAsyncKeyState(0x11) & 0x8000
                    and self.user32.GetAsyncKeyState(0x47) & 0x8000)


class ConsoleHotkey:
    """Coalescing, edge-triggered Ctrl+G listener for an interactive console.

    ``pending`` never consumes a request; ``consume`` atomically clears it.
    ``paused`` suppresses key presses during input and maintenance, including a
    key held down when the pause ends. Inject ``key_state`` for offline tests.
    On unsupported terminals/platforms ``start`` returns False; explicit CLI assistance remains available without this optional listener.
    """

    def __init__(self, enabled=True, *, key_state=None, poll_interval_s=0.025,
                 interactive=None):
        if isinstance(poll_interval_s, bool) or not isinstance(poll_interval_s, (int, float)) or not 0 < poll_interval_s <= 1:
            raise ValueError("poll_interval_s must be in (0, 1]")
        self.enabled = bool(enabled)
        self.available = False
        self.error = None
        self.key_state = key_state
        self.interactive = interactive
        self.poll_interval_s = poll_interval_s
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._requested = False
        self._last_down = False
        self._pause_count = 0

    def start(self):
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.available
            if not self.enabled or self.interactive is False:
                return False
            if self.key_state is None:
                try:
                    if self.interactive is None and (sys.stdin is None or not sys.stdin.isatty()):
                        return False
                    if os.name == "posix":
                        from .posix_console import PosixKeyState
                        self.key_state = PosixKeyState()
                    elif os.name == "nt":
                        self.key_state = _WindowsKeyState()
                    else:
                        return False
                except (AttributeError, OSError) as exc:
                    self.error = str(exc)
                    return False
            self._stop.clear()
            self._requested = False
            self._last_down = False
            self.available = True
            self._thread = threading.Thread(target=self._listen, name="harness-console-hotkey", daemon=True)
            self._thread.start()
            return True

    def _poll_once(self):
        # 与 paused()/consume() 使用同一把锁，按键不会落到输入对话的间隙中。
        with self._lock:
            if self._pause_count:
                return
            down = bool(self.key_state())
            if down and (not self._last_down or (getattr(self.key_state, "pulses", False) is True)):
                self._requested = True
            self._last_down = down

    def _listen(self):
        try:
            while not self._stop.is_set():
                self._poll_once()
                self._stop.wait(self.poll_interval_s)
        except Exception as exc:
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
                self.available = False
                if (getattr(self.key_state, "pulses", False) is True):
                    self.key_state.close()

    def pending(self):
        with self._lock:
            return self._requested

    def consume(self):
        with self._lock:
            requested = self._requested
            self._requested = False
            return requested

    @contextmanager
    def paused(self):
        with self._lock:
            if not self._pause_count and (getattr(self.key_state, "pulses", False) is True):
                self.key_state.pause()
            self._pause_count += 1
        try:
            yield
        finally:
            with self._lock:
                self._pause_count -= 1
                if self.key_state is not None:
                    try:
                        if (getattr(self.key_state, "pulses", False) is True):
                            if not self._pause_count:
                                self.key_state.resume()
                            self._last_down = False
                        else:
                            # Suppress a held Windows key after dialogue input.
                            self._last_down = bool(self.key_state())
                    except Exception as exc:
                        self.error = f"{type(exc).__name__}: {exc}"
                        self.available = False
                        self._stop.set()

    def close(self):
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)
        with self._lock:
            if (getattr(self.key_state, "pulses", False) is True):
                self.key_state.close()
                self.key_state = None
            self.available = False
            self._requested = False
