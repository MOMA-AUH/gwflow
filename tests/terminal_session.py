"""Drive an installed command with a real controlling terminal and keyboard."""

import errno
import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import termios
import time


def _control_terminal():
    os.setsid()
    fcntl.ioctl(1, termios.TIOCSCTTY, 0)


def rendered_lines(output):
    """Apply line-oriented Rich cursor/erase controls, retaining scrollback.

    Call with a terminal wide enough to avoid automatic line wrapping. This
    deliberately rejects other controls rather than modeling a full TUI.
    """
    lines = [[]]
    row = column = 0
    for part in re.split(r'(\x1b\[[0-?]*[ -/]*[@-~]|\r|\n)', output):
        if part == '\r':
            column = 0
        elif part == '\n':
            row += 1
            if row == len(lines):
                lines.append([])
        elif part.startswith('\x1b['):
            if part.endswith('m') or part in ('\x1b[?25h', '\x1b[?25l'):
                continue
            if part.endswith('A'):
                row = max(0, row - int(part[2:-1] or '1'))
            elif part == '\x1b[2K':
                lines[row] = []
            elif part in ('\x1b[K', '\x1b[0K'):
                del lines[row][column:]
            else:
                raise AssertionError(f'Unsupported terminal control: {part!r}')
        else:
            for character in part:
                while len(lines[row]) <= column:
                    lines[row].append(' ')
                lines[row][column] = character
                column += 1
    return '\n'.join(''.join(line) for line in lines)


class TerminalSession:
    def __init__(self, command, *, cwd, env=None, size=(80, 24), term='xterm-256color', encoding='utf-8',
                 input_mode='terminal'):
        self.master, slave = pty.openpty()
        width, height = size
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
        environment = {**os.environ, 'NO_COLOR': '', **(env or {}),
                       'TERM': term, 'PYTHONIOENCODING': encoding}
        environment.pop('COLUMNS', None)
        environment.pop('LINES', None)
        input_fd = (os.open(os.ttyname(slave), os.O_WRONLY) if input_mode == 'write-only'
                    else subprocess.DEVNULL if input_mode == 'unavailable' else slave)
        self.process = subprocess.Popen(command, cwd=cwd, env=environment,
                                        stdin=input_fd, stdout=slave, stderr=slave,
                                        preexec_fn=_control_terminal)
        if input_mode == 'write-only':
            os.close(input_fd)
        os.close(slave)
        self.output = bytearray()

    @property
    def text(self):
        return re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output.decode('utf-8', errors='replace'))

    def read(self, timeout=0.1):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output.extend(os.read(self.master, 65536))
            except OSError as error:
                if error.errno != errno.EIO:
                    raise

    def wait_for(self, text, *, timeout=20, after=0):
        deadline = time.monotonic() + timeout
        while text not in re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', self.output[after:].decode('utf-8', errors='replace')):
            self.read()
            if self.process.poll() is not None or time.monotonic() >= deadline:
                raise AssertionError(f'Terminal did not show {text!r}:\n{self.text}')

    def send(self, keys):
        checkpoint = len(self.output)
        os.write(self.master, keys.encode())
        return checkpoint

    def resize(self, width, height):
        fcntl.ioctl(self.master, termios.TIOCSWINSZ, struct.pack('HHHH', height, width, 0, 0))
        self.process.send_signal(signal.SIGWINCH)

    def finish(self, *, timeout=10):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None:
            self.read()
            if time.monotonic() >= deadline:
                raise AssertionError(f'Terminal command did not exit:\n{self.text}')
        self.read()
        return self.process.returncode

    def close(self):
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        os.close(self.master)

