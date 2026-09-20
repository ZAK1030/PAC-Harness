"""Keyboard request tests using fake key state, without a console or robot."""
import threading
import unittest
from unittest.mock import Mock, patch

from pac_harness.console_hotkey import ConsoleHotkey, _WindowsKeyState


class ConsoleHotkeyTests(unittest.TestCase):
    def setUp(self):
        self.down = False
        self.listener = ConsoleHotkey(key_state=lambda: self.down)
        self.addCleanup(self.listener.close)

    def poll(self, down):
        self.down = down
        self.listener._poll_once()

    def test_press_is_edge_triggered_and_pending_does_not_consume(self):
        self.poll(False)
        self.assertFalse(self.listener.pending())
        self.poll(True)
        self.assertTrue(self.listener.pending())
        self.assertTrue(self.listener.pending())
        self.assertTrue(self.listener.consume())
        self.assertFalse(self.listener.consume())
        self.poll(True)
        self.assertFalse(self.listener.pending())
        self.poll(False)
        self.poll(True)
        self.assertTrue(self.listener.consume())

    def test_multiple_presses_coalesce_to_one_dialogue(self):
        for _ in range(5):
            self.poll(True)
            self.poll(False)
        self.assertTrue(self.listener.consume())
        self.assertFalse(self.listener.consume())

    def test_paused_input_suppresses_presses_until_release(self):
        with self.listener.paused():
            self.poll(True)
            self.assertFalse(self.listener.pending())
        self.poll(True)
        self.assertFalse(self.listener.pending())
        self.poll(False)
        self.poll(True)
        self.assertTrue(self.listener.consume())

    def test_even_short_pause_without_poll_does_not_retrigger_held_key(self):
        with self.listener.paused():
            self.down = True
        self.poll(True)
        self.assertFalse(self.listener.pending())

    def test_nested_pause_and_exception_restore_listener(self):
        with self.assertRaises(RuntimeError):
            with self.listener.paused():
                with self.listener.paused():
                    self.poll(True)
                self.poll(False)
                self.poll(True)
                self.assertFalse(self.listener.pending())
                raise RuntimeError("dialogue error")
        self.poll(False)
        self.poll(True)
        self.assertTrue(self.listener.consume())

    def test_pause_does_not_lose_preexisting_request(self):
        self.poll(True)
        with self.listener.paused():
            self.poll(False)
        self.assertTrue(self.listener.consume())

    def test_start_is_idempotent_and_close_stops_thread(self):
        called = threading.Event()

        def key_state():
            called.set()
            return True

        listener = ConsoleHotkey(key_state=key_state)
        self.addCleanup(listener.close)
        self.assertTrue(listener.start())
        first_thread = listener._thread
        self.assertTrue(listener.start())
        self.assertIs(listener._thread, first_thread)
        self.assertTrue(called.wait(1))
        self.assertTrue(listener.pending())
        listener.close()
        self.assertFalse(first_thread.is_alive())
        self.assertFalse(listener.available)
        self.assertFalse(listener.pending())

    def test_disabled_or_noninteractive_does_not_poll(self):
        for settings in ({"enabled": False}, {"interactive": False}):
            with self.subTest(settings=settings):
                backend = Mock(return_value=True)
                listener = ConsoleHotkey(key_state=backend, **settings)
                self.assertFalse(listener.start())
                self.assertFalse(listener.available)
                listener.close()
                backend.assert_not_called()

    def test_missing_console_degrades_without_touching_input(self):
        with patch("pac_harness.console_hotkey.sys.stdin") as stdin:
            stdin.isatty.return_value = False
            listener = ConsoleHotkey()
            self.assertFalse(listener.start())
            stdin.read.assert_not_called()
            stdin.readline.assert_not_called()

    def test_backend_failure_disables_listener_without_interrupting_control_loop(self):
        listener = ConsoleHotkey(key_state=Mock(side_effect=OSError("lost console")))
        self.addCleanup(listener.close)
        listener.start()
        listener._thread.join(timeout=1)
        self.assertFalse(listener.available)
        self.assertIn("lost console", listener.error)
        self.assertFalse(listener.pending())

    def test_pause_exit_backend_failure_does_not_mask_dialogue_exception(self):
        listener = ConsoleHotkey(key_state=Mock(side_effect=OSError("lost console")))
        with self.assertRaisesRegex(ValueError, "original"):
            with listener.paused():
                raise ValueError("original")
        self.assertFalse(listener.available)
        self.assertIn("lost console", listener.error)

    def test_invalid_poll_period_rejected(self):
        for value in (True, 0, -1, 2, "0.1", float("nan")):
            with self.subTest(value=value), self.assertRaises(ValueError):
                ConsoleHotkey(poll_interval_s=value)


class WindowsKeyStateTests(unittest.TestCase):
    def backend(self, *, visible=True):
        user32, kernel32 = Mock(), Mock()
        kernel32.GetConsoleWindow.return_value = 101
        user32.IsWindowVisible.return_value = visible
        user32.GetForegroundWindow.return_value = 101 if visible else 202
        user32.GetAsyncKeyState.return_value = 0x8000
        with patch("ctypes.WinDLL", side_effect=[user32, kernel32], create=True):
            keyboard = _WindowsKeyState()
        return keyboard, user32

    def test_classic_console_requires_foreground_and_ctrl_g(self):
        keyboard, user32 = self.backend()
        self.assertTrue(keyboard())
        self.assertEqual([call.args[0] for call in user32.GetAsyncKeyState.call_args_list], [0x11, 0x47])
        user32.GetForegroundWindow.return_value = 999
        user32.GetAsyncKeyState.reset_mock()
        self.assertFalse(keyboard())
        user32.GetAsyncKeyState.assert_not_called()

    def test_ctrl_c_does_not_trigger_dialogue(self):
        keyboard, user32 = self.backend()
        user32.GetAsyncKeyState.side_effect = lambda key: 0x8000 if key in {0x11, 0x43} else 0
        self.assertFalse(keyboard())

    def test_pseudoconsole_pins_foreground_host_window(self):
        keyboard, user32 = self.backend(visible=False)
        self.assertTrue(keyboard())
        user32.GetForegroundWindow.return_value = 303
        self.assertFalse(keyboard())
        user32.GetForegroundWindow.return_value = 202
        self.assertTrue(keyboard())


if __name__ == "__main__":
    unittest.main()
