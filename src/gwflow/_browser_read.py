"""Stoppable, read-only file operations for the interactive evidence views."""

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile
import time

WINDOW_BYTES = 65536
WINDOW_LINES = 200
ESCAPES = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\|$)|[ -/]*[@-~])')
SGR = re.compile(r'\x1b\[(?:[0-9]{0,3};)*[0-9]{0,3}m')


def display_text(data):
    notices = []
    try:
        decoded = data.decode('utf-8')
    except UnicodeDecodeError:
        decoded = data.decode('utf-8', errors='replace')
        notices.append('Undecodable UTF-8 bytes shown as �.')
    controls = False

    def visible(value):
        nonlocal controls
        result = []
        for char in value:
            code = ord(char)
            if code < 32 and char not in '\n\t' or 127 <= code < 160:
                controls = True
                result.append(chr(0x2400 + code) if code < 32 else '␡' if code == 127 else '�')
            else:
                result.append(char)
        return ''.join(result)

    parts, position = [], 0
    for match in ESCAPES.finditer(decoded):
        parts.append(visible(decoded[position:match.start()]))
        sequence = match.group()
        if len(sequence) <= 128 and SGR.fullmatch(sequence):
            parts.append(sequence)
        else:
            controls = True
        position = match.end()
    parts.append(visible(decoded[position:]))
    if controls:
        notices.append('Terminal controls omitted or shown visibly.')
    return ''.join(parts), notices


class ReadOperation:
    """Own one file read, keeping blocking filesystem calls off the UI thread."""

    def __init__(self, request):
        self.directory = tempfile.TemporaryDirectory(prefix='gwflow-read-')
        self.root = Path(self.directory.name)
        with (self.root / 'result').open('wb') as output, (self.root / 'errors').open('wb') as errors:
            self.process = subprocess.Popen([sys.executable, '-m', 'gwflow._browser_read', json.dumps(request)],
                                            stdin=subprocess.DEVNULL, stdout=output, stderr=errors,
                                            start_new_session=True)

    def completed(self):
        return self.process.poll() is not None

    def outcome(self):
        if self.process.returncode:
            return {'error': (self.root / 'errors').read_text(errors='replace') or 'Read process failed.'}
        return json.loads((self.root / 'result').read_text())

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.directory.cleanup()


def read_log(path, *, direction='backward', offset=None, previous=None):
    if path is None:
        return {'error': 'No recorded submission for this stream.'}
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode):
            return {'error': 'Log path is not a regular file: ' + path}
        if previous is not None:
            if [metadata.st_dev, metadata.st_ino] != previous['identity']:
                return {'error': 'Log replaced. Press u to reload the selected path.'}
            if metadata.st_size < previous['size']:
                return {'error': 'Log truncated. Press u to reload the selected path.'}
            before = os.pread(fd, previous['end'] - previous['start'], previous['start'])
            if hashlib.sha256(before).hexdigest() != previous['digest']:
                return {'error': 'Log truncated or rewritten. Press u to reload the selected path.'}
        if direction == 'forward' and offset is not None and offset >= metadata.st_size:
            direction, offset = 'backward', None
        position = metadata.st_size if offset is None else min(max(0, offset), metadata.st_size)
        start = max(0, position - WINDOW_BYTES) if direction == 'backward' else position
        count = position - start if direction == 'backward' else WINDOW_BYTES
        # Four context bytes on each side locate UTF-8 boundaries and the
        # preceding newline without searching outside the bounded window.
        read_start = max(0, start - 4)
        buffer = os.pread(fd, count + start - read_start + 4, read_start)
        left, right = start - read_start, min(start - read_start + count, len(buffer))
        for _ in range(3):
            if left > 0 and left < len(buffer) and 0x80 <= buffer[left] < 0xc0:
                left -= 1
        for _ in range(3):
            if right < len(buffer) and 0x80 <= buffer[right] < 0xc0:
                right += 1
        start = read_start + left
        data = buffer[left:right]
        line_limited = False
        if direction == 'backward':
            lines = data.split(b'\n')
            selected = b'\n'.join(lines[-(WINDOW_LINES + int(data.endswith(b'\n'))):])
            line_limited = len(selected) < len(data)
            start += len(data) - len(selected)
            data = selected
        else:
            lines = data.split(b'\n', WINDOW_LINES)
            if len(lines) > WINDOW_LINES:
                data = data[:len(data) - len(lines[-1])]
                line_limited = True
        end = start + len(data)
        content, notices = display_text(data)
        if not line_limited and (start > 0 if direction == 'backward' else end < metadata.st_size):
            notices.insert(0, '64 KiB byte limit: showing a smaller portion.')
        return dict(content=content, notices=notices, finished=time.time(),
                    size=metadata.st_size, start=start, end=end,
                    continued_before=bool(data and start > 0 and buffer[start - read_start - 1] != 10),
                    continued_after=bool(data and end < metadata.st_size and not data.endswith(b'\n')),
                    identity=[metadata.st_dev, metadata.st_ino], digest=hashlib.sha256(data).hexdigest())
    finally:
        os.close(fd)


def read_history(storage, task):
    from . import admission
    from ._frontend import _submission_guard
    from .lifecycle import Store

    with _submission_guard(storage['working_dir'], waiting_message=None):
        store = Store(**storage)
        selected = store.recorded_tasks(names={task})
        current = selected[0] if selected else None
        current_id = current['attempt'] if current else None
        attempts = store.recorded_attempts(names={task})
        attempts.sort(key=lambda attempt: (attempt['attempt'] != current_id, attempt['attempt']))
        groups = []
        for attempt in attempts:
            observations = admission.observe_submissions(store, attempt, None, 'recorded')
            jobs = []
            for item in observations:
                state = {'complete': 'completed', 'cancelled': 'canceled', 'uncertain': 'unknown'}.get(item.state, item.state)
                jobs.append(dict(local=item.local, name=f'{task}__{item.local}', submission=item.submission,
                                 state=state, observed_state=state, recorded_state=item.state,
                                 requires_repetition=False, backend_id=item.job_id, backend=item.intent['backend'],
                                 logs={stream: str(store.working_dir / '.gwf' / 'logs' / f'{item.submission}.{stream}')
                                       for stream in ('stdout', 'stderr')}))
            if attempt['attempt'] == current_id:
                current_submissions = {item.submission for item in admission.observe(store, attempt, None, 'recorded').values()}
                groups.append(dict(label='Current recorded attempt ' + current_id, attempt=current_id,
                                   jobs=[job for job in jobs if job['submission'] in current_submissions]))
                earlier = [job for job in jobs if job['submission'] not in current_submissions]
                if earlier:
                    groups.append(dict(label='Earlier retries in current attempt ' + current_id,
                                       attempt=current_id, jobs=earlier))
            else:
                groups.append(dict(label='Previous attempt ' + attempt['attempt'], attempt=attempt['attempt'], jobs=jobs))
    return dict(groups=groups, current_attempt=current_id, finished=time.time())


if __name__ == '__main__':
    from .diagnostics import error_message
    try:
        request = json.loads(sys.argv[1])
        kind = request.pop('kind', 'log')
        if kind == 'log':
            result = read_log(**request)
        elif kind == 'history':
            result = read_history(**request)
        else:
            raise ValueError('Unknown evidence read.')
    except Exception as error:
        result = {'error': error_message(error, 'Evidence read')}
    print(json.dumps(result))
