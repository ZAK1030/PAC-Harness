"""POSIX TTY input shared by the listener and foreground dialogue."""
import codecs
from collections import deque
from contextlib import contextmanager
import os
import select
import sys

pending = deque()


class PosixKeyState:
    pulses = True

    def __init__(self, fd=None):
        self.fd = sys.stdin.fileno() if fd is None else fd
        self.saved = None
        self.resume()

    def resume(self):
        import termios
        if self.saved is not None:
            return
        if not os.isatty(self.fd) or os.tcgetpgrp(self.fd) != os.getpgrp():
            raise OSError("Ctrl+G requires a foreground TTY")
        saved = termios.tcgetattr(self.fd)
        mode = [*saved[:6], list(saved[6])]
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(self.fd, termios.TCSANOW, mode)
        self.saved = saved

    def pause(self):
        import termios
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
            self.saved = None

    close = pause

    def __call__(self):
        if self.saved is None or os.tcgetpgrp(self.fd) != os.getpgrp():
            return False
        if not select.select([self.fd], [], [], 0)[0]:
            return False
        char = os.read(self.fd, 1)
        if not char:
            raise EOFError("terminal disconnected")
        if char == b'\x07':
            return True
        pending.append(char)
        return False


@contextmanager
def dialogue_reader():
    terminal = PosixKeyState()
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')

    def read():
        while True:
            raw = pending.popleft() if pending else os.read(terminal.fd, 1)
            if not raw or raw == b'\x04':
                return ''
            # Consume ANSI cursor/function sequences instead of inserting them.
            if raw == b'\x1b':
                for _ in range(32):
                    if not select.select([terminal.fd], [], [], .02)[0]:
                        break
                    part = os.read(terminal.fd, 1)
                    if part not in (b'[', b'O') and part and 0x40 <= part[0] <= 0x7e:
                        break
                continue
            char = decoder.decode(raw)
            if char:
                return char
    try:
        yield read
    finally:
        terminal.close()
