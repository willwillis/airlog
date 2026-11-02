"""FFmpeg decodes only. This process never needs the database or recognizer."""
import datetime as dt
import json
import logging
import os
import random
import selectors
import shutil
import signal
import subprocess
import threading
import time
import uuid
import wave

from .storage import atomic_json, read_json, singleton, status

log = logging.getLogger(__name__)


def event(config, kind, detail):
    record = dict(type='event', id=uuid.uuid4().hex, observed=time.time(), kind=kind, detail=detail)
    atomic_json(config.audio / 'queue' / (record['id'] + '.json'), record)


def finish_capture(config, manifest, recovered=False):
    raw = config.audio / 'capture' / (manifest['id'] + '.pcm')
    if not raw.exists():
        return
    size = raw.stat().st_size // 2 * 2
    if size == 0:
        raw.unlink()
        (raw.with_suffix('.json')).unlink(missing_ok=True)
        return
    folder = config.audio / 'recordings' / dt.datetime.fromtimestamp(manifest['started'], dt.timezone.utc).strftime('%Y-%m/%d')
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (manifest['id'] + '.wav')
    temp = path.with_suffix('.wav.tmp')
    with wave.open(str(temp), 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(config.sample_rate)
        with raw.open('rb') as f:
            w.writeframes(f.read(size))
    with temp.open('rb') as f:
        os.fsync(f.fileno())
    os.replace(temp, path)
    record = dict(manifest, path=str(path.relative_to(config.audio)), duration=size / (2 * config.sample_rate),
                  bytes=path.stat().st_size, recovered=int(recovered))
    atomic_json(path.with_suffix('.json'), record)
    atomic_json(config.audio / 'queue' / (manifest['id'] + '.json'), record)
    raw.unlink()
    (raw.with_suffix('.json')).unlink(missing_ok=True)
    return record


class Recorder:
    def __init__(self, config):
        self.config = config
        self.stop = threading.Event()
        self.process = None
        self.current = None
        self.output = None
        self.last_audio = None
        self.last_complete = None

    def report(self, state, **extra):
        try:
            status(self.config, 'recorder', state=state, last_audio=self.last_audio,
                   last_complete=self.last_complete, **extra)
        except OSError:
            log.exception('Cannot write recorder heartbeat')

    def close_chunk(self):
        if self.output:
            self.output.flush()
            os.fsync(self.output.fileno())
            self.output.close()
            self.output = None
        if self.current:
            record = finish_capture(self.config, self.current)
            if record:
                self.last_complete = record['started'] + record['duration']
            self.current = None

    def new_chunk(self, session, started):
        self.current = dict(id=uuid.uuid4().hex, session=session, started=started)
        p = self.config.audio / 'capture' / (self.current['id'] + '.json')
        atomic_json(p, self.current)
        self.output = p.with_suffix('.pcm').open('wb', buffering=0)

    def capture(self):
        config = self.config
        self.close_chunk()  # Retry finalization after a transient filesystem failure.
        session = uuid.uuid4().hex
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin',
                   '-rw_timeout', '20000000', '-i', config.stream, '-vn',
                   '-ac', '1', '-ar', str(config.sample_rate), '-f', 's16le', 'pipe:1']
        # No internal reconnect: each connection gets a new, explicitly timed session.
        self.process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=None, bufsize=0)
        selector = selectors.DefaultSelector()
        selector.register(self.process.stdout, selectors.EVENT_READ)
        last_read = time.monotonic()
        last_report = 0
        anchor = None
        samples = 0
        chunk_bytes = 0
        target = config.chunk_seconds * config.sample_rate * 2
        try:
            while not self.stop.is_set():
                if time.monotonic() - last_report >= 2:
                    if shutil.disk_usage(config.audio).free < config.min_free_bytes:
                        raise RuntimeError('Disk reserve reached; capture paused, protected audio retained')
                    self.report('recording' if anchor else 'connecting', session=session)
                    last_report = time.monotonic()
                if not selector.select(timeout=1):
                    if time.monotonic() - last_read > 30:
                        raise RuntimeError('Stream stalled for 30 seconds')
                    continue
                data = os.read(self.process.stdout.fileno(), 32768)
                if not data:
                    raise RuntimeError('Stream disconnected')
                last_read = time.monotonic()
                self.last_audio = time.time()
                if anchor is None:
                    anchor = self.last_audio
                    event(config, 'connected', json.dumps({'session': session, 'first_audio': anchor,
                        'previous_audio_end': self.last_complete}))
                while data:
                    if self.current is None:
                        self.new_chunk(session, anchor + samples / (config.sample_rate * 2))
                        chunk_bytes = 0
                    part, data = data[:target - chunk_bytes], data[target - chunk_bytes:]
                    self.output.write(part)
                    chunk_bytes += len(part)
                    samples += len(part)
                    if chunk_bytes == target:
                        self.close_chunk()
        finally:
            selector.close()
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process.stdout.close()
            self.process = None
            self.close_chunk()

    def run(self):
        with singleton(self.config.audio / '.recorder.lock'):
            for file in (self.config.audio / 'capture').glob('*.json'):
                manifest = read_json(file)
                if manifest:
                    recovered = finish_capture(self.config, manifest, recovered=True)
                    if recovered:
                        self.last_complete = max(self.last_complete or 0, recovered['started'] + recovered['duration'])
            previous = read_json(self.config.data / 'recorder.json', {})
            self.last_complete = max(self.last_complete or 0, previous.get('last_complete') or 0) or None
            event(self.config, 'recorder_start', 'Recorder started; any downtime is not recoverable from the stream')
            delay = 1
            while not self.stop.is_set():
                began = time.monotonic()
                try:
                    self.capture()
                except Exception as exc:
                    log.exception('Capture interrupted')
                    self.report('retrying', error=str(exc), retry_seconds=delay)
                    try:
                        event(self.config, 'capture_interrupted', str(exc))
                    except OSError:
                        log.exception('Cannot persist interruption event')
                if time.monotonic() - began > 60:
                    delay = 1
                self.stop.wait(delay + random.random())
                delay = min(60, delay * 2)
            self.report('stopped')


def main(config):
    recorder = Recorder(config)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: recorder.stop.set())
    recorder.run()
