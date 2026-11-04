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


def delete_audio(config, c, row, now, reason):
    # Durable intent precedes deletion. Recovery replays it after an interrupted transaction.
    path = audio_path(config, row['path'])
    tombstone = config.audio / 'queue' / (row['id'] + '.deleted')
    atomic_json(tombstone, {'id': row['id'], 'deleted_at': now, 'path': row['path'], 'reason': reason})
    path.unlink(missing_ok=True)
    c.execute('UPDATE chunks SET deleted_at=? WHERE id=?', (now, row['id']))
    c.execute('INSERT INTO events VALUES (?,?,?,?)', ('delete:' + row['id'], now, 'audio_deleted',
              json.dumps({'chunk': row['id'], 'reason': reason})))


def recover_deletions(config):
    for file in (config.audio / 'queue').glob('*.deleted'):
        record = read_json(file)
        if record:
            with db(config) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM chunks WHERE id=?', (record['id'],)).fetchone()
                if not row:
                    continue
                path = audio_path(config, record['path'])
                # A Keep review made after a rolled-back deletion wins while audio still exists.
                if row['review'] != 'keep' or not path.exists():
                    path.unlink(missing_ok=True)
                    c.execute('UPDATE chunks SET deleted_at=? WHERE id=?', (record['deleted_at'], record['id']))
                    c.execute('INSERT OR IGNORE INTO events VALUES (?,?,?,?)',
                              ('delete:' + record['id'], record['deleted_at'], 'audio_deleted',
                               json.dumps({'chunk': record['id'], 'reason': record.get('reason', 'recovered_deletion')})))
            file.unlink(missing_ok=True)


def delete_unidentified(config):
    removed = 0
    now = time.time()
    with db(config) as c:
        # Serialize selection and deletion with Keep, Retry, and worker updates.
        c.execute('BEGIN IMMEDIATE')
        candidates = list(c.execute("""SELECT * FROM chunks
            WHERE state='unknown' AND review!='keep' AND deleted_at IS NULL"""))
        for row in candidates:
            delete_audio(config, c, row, now, 'manual_unidentified')
            removed += 1
    # Only acknowledge tombstones after the SQLite commit.
    recover_deletions(config)
    return removed


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
