"""Durable audio manifests are independent of SQLite and recognition."""
import contextlib
import fcntl
import json
import os
import sqlite3
import time
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS chunks (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, path TEXT NOT NULL,
 started REAL NOT NULL, duration REAL NOT NULL, bytes INTEGER NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', artist TEXT, title TEXT, track_key TEXT,
 offset REAL, attempts INTEGER NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
 error TEXT, review TEXT NOT NULL DEFAULT 'auto', deleted_at REAL,
 retention_checked REAL NOT NULL DEFAULT 0, recovered INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS chunk_time ON chunks(started);
CREATE INDEX IF NOT EXISTS chunk_queue ON chunks(state,next_attempt);
CREATE TABLE IF NOT EXISTS observations (
 id INTEGER PRIMARY KEY, chunk_id TEXT, observed REAL NOT NULL, provider TEXT NOT NULL,
 result TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS observation_chunk ON observations(chunk_id);
CREATE TABLE IF NOT EXISTS metadata (
 id INTEGER PRIMARY KEY, observed REAL NOT NULL, artist TEXT, title TEXT, raw TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS metadata_time ON metadata(observed);
CREATE TABLE IF NOT EXISTS events (
 id TEXT PRIMARY KEY, observed REAL NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_time ON events(observed);
PRAGMA user_version=1;
'''

def atomic_json(path, obj):
    path = Path(path)
    temp = path.with_suffix(path.suffix + '.tmp')
    with temp.open('w') as f:
        json.dump(obj, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


@contextlib.contextmanager
def singleton(path):
    with Path(path).open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextlib.contextmanager
def db(config):
    connection = sqlite3.connect(config.data / 'radio.sqlite3', timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA foreign_keys=ON')
    connection.execute('PRAGMA busy_timeout=10000')
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize(config):
    with db(config) as c:
        c.execute('PRAGMA journal_mode=WAL')
        c.executescript(SCHEMA)


def audio_path(config, relative):
    p = (config.audio / relative).resolve()
    if not p.is_relative_to(config.audio.resolve()) or p == config.audio.resolve():
        raise ValueError('Invalid audio path')
    return p


def ingest(config, rebuild=False):
    # Queue files are acknowledgement messages; immutable sidecars allow rebuilding.
    files = list((config.audio / 'queue').glob('*.json'))
    if rebuild:
        files += list((config.audio / 'recordings').glob('*/*/*.json'))
    count = 0
    for file in files:
        record = read_json(file)
        if not record:
            continue
        with db(config) as c:
            if record.get('type') == 'event':
                c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                          (record['id'], record['observed'], record['kind'], record['detail']))
            else:
                p = audio_path(config, record['path'])
                state = 'pending' if p.exists() else 'missing'
                c.execute('''INSERT OR IGNORE INTO chunks
                (id,session,path,started,duration,bytes,state,recovered) VALUES (?,?,?,?,?,?,?,?)''',
                          (record['id'], record['session'], record['path'], record['started'],
                           record['duration'], record['bytes'], state, record.get('recovered', 0)))
                count += 1
        if file.parent == config.audio / 'queue':
            file.unlink(missing_ok=True)
    return count


def status(config, service, **values):
    atomic_json(config.data / f'{service}.json', {'updated': time.time(), **values})
