"""ToUser console input tests; no interactive console, model, or hardware."""
from collections import deque
import unittest
from unittest.mock import Mock, patch

from pac_harness import dialogue_input


class DialogueInputTests(unittest.TestCase):
    def setUp(self):
        dialogue_input._pending_chars.clear()
        self.addCleanup(dialogue_input._pending_chars.clear)

    def read(self, text):
        output = []
        stream = iter(text)
        result = dialogue_input._read_line("你：", lambda: next(stream), output.append)
        return result, "".join(output)

    def backend(self, text):
        queue = deque(text)
        return Mock(kbhit=lambda: bool(queue), getwch=lambda: queue.popleft())

    def test_chinese_unicode_paste_and_normal_enter(self):
        result, output = self.read("请修改长期记忆和代码。\r")
        self.assertEqual(result, "请修改长期记忆和代码。")
        self.assertEqual(output, "你：请修改长期记忆和代码。\n")

    def test_ctrl_g_returns_immediately_and_discards_partial_request(self):
        result, output = self.read("暂未提交\x07")
        self.assertEqual(result, "\x07")
        self.assertTrue(output.endswith("\n"))

    def test_ctrl_c_interrupts_without_enter(self):
        with self.assertRaises(KeyboardInterrupt):
            self.read("hello\x03")

    def test_ctrl_z_signals_eof(self):
        with self.assertRaises(EOFError):
            self.read("\x1a")

    def test_ascii_and_chinese_backspace_echo_width(self):
        result, output = self.read("ab\b你\b好\r")
        self.assertEqual(result, "a好")
        self.assertIn("你\b \b\b \b好", output)

    def test_backspace_empty_line_is_harmless(self):
        self.assertEqual(self.read("\b\bC\r"), ("C", "你：C\n"))

    def test_extended_arrow_and_function_keys_do_not_enter_text(self):
        result, _ = self.read("a\xe0K\x00;b\r")
        self.assertEqual(result, "ab")

    def test_astral_unicode_surrogates_combined_before_echo(self):
        result, output = self.read("\ud83d\ude80\r")
        self.assertEqual(result, "🚀")
        self.assertEqual(output, "你：🚀\n")
        output.encode("utf-8")

    def test_malformed_surrogate_does_not_hide_return_key(self):
        result, output = self.read("\ud83d\x07")
        self.assertEqual(result, "\x07")
        output.encode("utf-8")

    def test_combining_accent_backspace_removes_visible_character(self):
        self.assertEqual(self.read("e\u0301\bC\r")[0], "C")

    def test_tabs_expand_and_unhandled_control_keys_do_not_corrupt_echo(self):
        self.assertEqual(self.read("a\t\x01b\r")[0], "a    b")

    def test_unsupported_console_uses_builtin_input_without_reading_chars(self):
        with patch.object(dialogue_input, "_windows_backend", return_value=None), \
                patch("builtins.input", return_value="plain text") as fallback:
            self.assertEqual(dialogue_input.read_dialogue_input("prompt"), "plain text")
        fallback.assert_called_once_with("prompt")

    def test_noninteractive_backend_does_not_access_win32_console(self):
        with patch.object(dialogue_input.os, "name", "nt"), \
                patch.object(dialogue_input.sys, "stdin") as stdin:
            stdin.isatty.return_value = False
            self.assertIsNone(dialogue_input._windows_backend())
            stdin.fileno.assert_not_called()

    def test_entry_key_drain_preserves_other_queued_text(self):
        backend = self.backend("\x07请修改\x07\r")
        with patch.object(dialogue_input, "_windows_backend", return_value=backend), \
                patch.object(dialogue_input, "_write_console"):
            dialogue_input.drain_entry_hotkey()
            self.assertEqual(dialogue_input.read_dialogue_input("你："), "请修改")

    def test_later_ctrl_g_is_not_discarded_by_prompt_reader(self):
        backend = self.backend("\x07")
        with patch.object(dialogue_input, "_windows_backend", return_value=backend), \
                patch.object(dialogue_input, "_write_console"):
            self.assertEqual(dialogue_input.read_dialogue_input("你："), "\x07")

    def test_polling_console_allows_python_interrupts_while_no_key_is_ready(self):
        backend = Mock(kbhit=Mock(return_value=False))
        with patch.object(dialogue_input, "_windows_backend", return_value=backend), \
                patch.object(dialogue_input, "_write_console"), \
                patch.object(dialogue_input.time, "sleep", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                dialogue_input.read_dialogue_input("你：")
        backend.getwch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
