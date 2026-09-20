import os
import subprocess
import sys
import unittest


@unittest.skipUnless(os.name == 'posix', 'requires POSIX PTY')
class PosixConsoleTests(unittest.TestCase):
    def test_real_tty_hotkey_pause_unicode_and_restore(self):
        # A controlling PTY is required; run in a child session so CI stdin may be a pipe.
        code = r'''
import os, pty, fcntl, termios, sys, time
master, slave = pty.openpty()
os.setsid()
fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
original = termios.tcgetattr(slave)
from pac_harness.console_hotkey import ConsoleHotkey
from pac_harness.posix_console import PosixKeyState, pending
from pac_harness.dialogue_input import read_dialogue_input
keyboard = PosixKeyState(slave)
listener = ConsoleHotkey(key_state=keyboard)
os.write(master, b'abc\x07')
for _ in range(4): listener._poll_once()
assert listener.consume()
assert b''.join(pending) == b'abc'
pending.clear()
with listener.paused():
    assert termios.tcgetattr(slave) == original
    os.dup2(slave, 0)
    os.dup2(slave, 1)
    os.write(master, '中文\n'.encode())
    assert read_dialogue_input('') == '中文'
    os.write(master, b'\x07')
    assert read_dialogue_input('') == '\x07'
try:
    with listener.paused(): raise ValueError('keep exception')
except ValueError: pass
else: raise AssertionError('exception swallowed')
listener.close()
assert termios.tcgetattr(slave) == original
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))

