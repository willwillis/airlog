import json
import logging
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import uuid

from .storage import audio_path, atomic_json, db, ingest, initialize, read_json, singleton, status

log = logging.getLogger(__name__)


def parse_metadata(value):
    """Keep the original payload even when a station uses an unrecognized shape."""
    if isinstance(value, dict):
        for key in ('now_playing', 'current', 'metadata'):
            if isinstance(value.get(key), dict):
                return parse_metadata(value[key])
        artist = value.get('artist')
        title = value.get('title') or value.get('song') or value.get('StreamTitle') or value.get('stream_title')
        if isinstance(title, dict):
            return parse_metadata(title)
        if isinstance(title, str):
            if not artist and ' - ' in title:
                return tuple(title.split(' - ', 1))
            return str(artist or ''), title
    if isinstance(value, str) and ' - ' in value:
        return tuple(value.split(' - ', 1))
    return '', ''


def metadata_loop(config, stop):
    while not stop.is_set():
        try:
            if not config.metadata_url:
                status(config, 'metadata', state='disabled')
                stop.wait(30)
                continue
            req = urllib.request.Request(config.metadata_url, headers={'User-Agent': 'Airlog/1.0', 'Accept': 'application/json'})
            with urllib.request.urlopen(req, timeout=15) as response:
                raw = response.read(65536).decode('utf-8', 'replace')
            try:
                value = json.loads(raw)
            except ValueError:
                value = raw
            artist, title = parse_metadata(value)
            observed = time.time()
            with db(config) as c:
                c.execute('INSERT INTO metadata(observed,artist,title,raw) VALUES (?,?,?,?)',
                          (observed, artist, title, raw))
            status(config, 'metadata', state='ok' if title else 'unparsed', artist=artist, title=title)
        except Exception as exc:
            log.warning('Metadata unavailable: %s', exc)
            try:
                status(config, 'metadata', state='error', error=str(exc)[:300])
            except OSError:
                log.exception('Cannot persist metadata status')
        stop.wait(config.metadata_interval)


def save_result(config, chunk, result, now=None):
    now = now or time.time()
    attempts = chunk['attempts'] + 1
    state = result['state']
    error = result.get('error')
    if state == 'unknown' and attempts < 2:
        state = 'retry'
    retry = now + min(3600, 60 * 2 ** min(attempts, 6)) if state in ('error', 'retry') else 0
    with db(config) as c:
        c.execute('INSERT INTO observations(chunk_id,observed,provider,result) VALUES (?,?,?,?)',
                  (chunk['id'], now, 'shazam', json.dumps(result)))
        c.execute('''UPDATE chunks SET state=?, artist=?, title=?, track_key=?, offset=?,
        attempts=?,next_attempt=?,error=? WHERE id=?''', (state, result.get('artist'), result.get('title'),
            result.get('track_key'), result.get('offset'), attempts, retry, error, chunk['id']))


def automatic_candidate(c, row, chunk_seconds):
    """A heuristic, NOT a guarantee against speech over music. Explicit opt-in only."""
    if row['state'] != 'matched' or row['offset'] is None or row['recovered']:
        return False
    before = list(c.execute('SELECT * FROM chunks WHERE started<? ORDER BY started DESC LIMIT 2', (row['started'],)))
    after = list(c.execute('SELECT * FROM chunks WHERE started>? ORDER BY started LIMIT 2', (row['started'],)))
    if len(before) != 2 or len(after) != 2:
        return False
    sequence = list(reversed(before)) + [row] + after
    for item in sequence:
        if (item['session'] != row['session'] or item['state'] != 'matched' or item['track_key'] != row['track_key']
                or item['offset'] is None or item['recovered'] or item['duration'] < chunk_seconds - .1
                or item['review'] == 'keep'):
            return False
        # The song-position estimate must advance in step with the captured audio.
        if abs((item['offset'] - row['offset']) - (item['started'] - row['started'])) > 4:
            return False
    return all(abs(b['started'] - (a['started'] + a['duration'])) < .5 for a, b in zip(sequence, sequence[1:]))


def retain(config, now=None):
    now = now or time.time()
    removed = 0
    with db(config) as c:
        c.execute('BEGIN IMMEDIATE')
        candidates = list(c.execute('''SELECT * FROM chunks WHERE deleted_at IS NULL AND review != 'keep'
            AND started+duration < ? AND (review='release' OR (state='matched' AND ?)) ORDER BY retention_checked,started LIMIT 1000''',
                                   (now - config.retention_hours * 3600, int(config.auto_delete))))
        for row in candidates:
            c.execute('UPDATE chunks SET retention_checked=? WHERE id=?', (now, row['id']))
            if row['review'] != 'release' and not (config.auto_delete and automatic_candidate(c, row, config.chunk_seconds)):
                continue
            path = audio_path(config, row['path'])
            # Durable intent precedes deletion. Recovery replays it if a crash interrupts the transaction.
            tombstone = config.audio / 'queue' / (row['id'] + '.deleted')
            reason = 'manual_release' if row['review'] == 'release' else 'automatic_heuristic'
            atomic_json(tombstone, {'id': row['id'], 'deleted_at': now, 'path': row['path'], 'reason': reason})
            path.unlink(missing_ok=True)
            c.execute('UPDATE chunks SET deleted_at=? WHERE id=?', (now, row['id']))
            c.execute('INSERT INTO events VALUES (?,?,?,?)', ('delete:' + row['id'], now, 'audio_deleted',
                      json.dumps({'chunk': row['id'], 'reason': reason})))
            removed += 1
    # Only acknowledge tombstones after the SQLite commit.
    recover_deletions(config)
    return removed


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


def run_recognition(config, row):
    path = audio_path(config, row['path'])
    if not path.exists():
        return dict(state='error', error='Audio file missing')
    try:
        result = subprocess.run([sys.executable, '-m', 'radio.recognize', str(path)],
                                capture_output=True, text=True, timeout=45)
        payload = json.loads(result.stdout)
        if payload.get('state') not in ('matched', 'unknown', 'error'):
            raise ValueError('Unexpected recognition response')
        return payload
    except subprocess.TimeoutExpired:
        return dict(state='error', error='Recognition timed out after 45 seconds')
    except Exception as exc:
        return dict(state='error', error=f'Recognition process failed: {exc}'[:500])


def main(config):
    initialize(config)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    with singleton(config.data / '.worker.lock'):
        ingest(config, rebuild=True)
        recover_deletions(config)
        meta = threading.Thread(target=metadata_loop, args=(config, stop), daemon=True)
        meta.start()
        next_request = 0
        next_retention = 0
        failures = 0
        last_error = None
        while not stop.is_set():
            try:
                ingest(config)
                if time.monotonic() >= next_retention:
                    retain(config)
                    next_retention = time.monotonic() + 60
                row = None
                if config.recognition != 'none':
                    with db(config) as c:
                        row = c.execute('''SELECT * FROM chunks WHERE deleted_at IS NULL AND
                         state IN ('pending','retry','error') AND next_attempt<=?
                         ORDER BY attempts,started LIMIT 1''', (time.time(),)).fetchone()
                status(config, 'worker', state='disabled' if config.recognition == 'none' else ('backoff' if failures else 'waiting'),
                       error=last_error, provider=config.recognition, retry_in=max(0, next_request - time.monotonic()))
                if row and time.monotonic() >= next_request:
                    status(config, 'worker', state='recognizing', provider=config.recognition, chunk=row['id'])
                    began = time.monotonic()
                    result = run_recognition(config, row)
                    save_result(config, row, result)
                    failures = failures + 1 if result['state'] == 'error' else 0
                    last_error = result.get('error')
                    cooldown = min(1800, 30 * 2 ** min(failures, 6)) if failures else config.request_interval
                    next_request = max(time.monotonic(), began + cooldown)
                    status(config, 'worker', state='backoff' if failures else 'ready', provider=config.recognition,
                           error=result.get('error'), retry_in=max(0, next_request - time.monotonic()))
            except Exception as exc:
                log.exception('Worker cycle failed; audio remains on disk')
                try:
                    status(config, 'worker', state='error', error=str(exc)[:300])
                except OSError:
                    pass
                stop.wait(10)
            stop.wait(2)
        meta.join(timeout=17)
        status(config, 'worker', state='stopped')
