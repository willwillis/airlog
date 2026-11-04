import base64
import csv
import hmac
import html
import io
import json
import logging
import mimetypes
import os
from pathlib import Path
import re
import shutil
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .storage import audio_path, db, delete_unidentified, initialize, read_json

STATIC = Path(__file__).resolve().parent.parent / 'static'
log = logging.getLogger(__name__)


def rows(c, sql, args=()):
    return [dict(r) for r in c.execute(sql, args)]


def play_runs(c, since, limit=100, offset=0):
    # Include unknown chunks in the window: a no-match must split two observed runs.
    # Recorder restarts change session IDs, but should not split a continuous match.
    items = rows(c, '''WITH prior AS (
      SELECT *, LAG(track_key) OVER w AS prev_key, LAG(state) OVER w AS prev_state,
        LAG(started+duration) OVER w AS prev_end FROM chunks WHERE started>=?
      WINDOW w AS (ORDER BY started,id)
    ), boundaries AS (
      SELECT *, CASE WHEN state='matched' AND prev_state='matched' AND track_key=prev_key AND ABS(started-prev_end)<0.5
        THEN 0 ELSE 1 END AS boundary FROM prior
    ), grouped AS (
      SELECT *, SUM(boundary) OVER (ORDER BY started,id) AS run FROM boundaries
    ) SELECT MIN(id) AS id, artist,title,track_key,MIN(started) AS started,
      MAX(started+duration) AS ended, SUM(duration) AS duration,COUNT(*) AS samples,
      SUM(CASE WHEN deleted_at IS NULL THEN 1 ELSE 0 END) AS available
      FROM grouped WHERE state='matched' GROUP BY run ORDER BY started DESC LIMIT ? OFFSET ?''',
                (since, limit, offset))
    # Artwork remains in recognition evidence even after the audio is deleted.
    # Read existing observations so older recordings need no migration/re-analysis.
    by_chunk = {item['id']: item for item in items}
    for item in items:
        item['artwork_url'] = None
    if by_chunk:
        placeholders = ','.join('?' for _ in by_chunk)
        evidence = c.execute(f'''SELECT chunk_id,result FROM observations
            WHERE chunk_id IN ({placeholders}) ORDER BY observed DESC,id DESC''', tuple(by_chunk))
        for observation in evidence:
            item = by_chunk[observation['chunk_id']]
            if item['artwork_url']:
                continue
            try:
                track = json.loads(observation['result'])['raw']['track']
                if str(track.get('key')) != item['track_key']:
                    continue
                images = track.get('images') or {}
                for name in ('coverart', 'coverarthq'):
                    url = images.get(name)
                    if not isinstance(url, str):
                        continue
                    parsed = urlsplit(url)
                    if parsed.scheme == 'https' and parsed.hostname and not parsed.username and not parsed.password:
                        item['artwork_url'] = url
                        break
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
    return items


def snapshot(config):
    now = time.time()
    with db(config) as c:
        totals = dict(c.execute('''SELECT COUNT(*) AS chunks,
          COALESCE(SUM(CASE WHEN deleted_at IS NULL THEN bytes ELSE 0 END),0) AS stored_bytes,
          COALESCE(SUM(CASE WHEN state='matched' THEN 1 ELSE 0 END),0) AS matched,
          COALESCE(SUM(CASE WHEN state IN ('pending','retry','error') AND deleted_at IS NULL THEN 1 ELSE 0 END),0) AS queued,
          COALESCE(SUM(CASE WHEN state='unknown' THEN 1 ELSE 0 END),0) AS unknown,
          COALESCE(SUM(CASE WHEN review='keep' THEN 1 ELSE 0 END),0) AS pinned,
          MIN(CASE WHEN state IN ('pending','retry','error') AND deleted_at IS NULL THEN started END) AS oldest_pending
          FROM chunks''').fetchone())
        totals['captured_today'] = c.execute('''SELECT COALESCE(SUM(MIN(started+duration,?)-MAX(started,?)),0)
            FROM chunks WHERE started+duration>? AND started<?''', (now, now-86400, now-86400, now)).fetchone()[0]
        latest = c.execute("SELECT * FROM chunks WHERE state='matched' ORDER BY started DESC LIMIT 1").fetchone()
        timeline = rows(c, 'SELECT id,started,duration,state,review,deleted_at FROM chunks WHERE started>? ORDER BY started', (now-7200,))
        events = rows(c, 'SELECT * FROM events ORDER BY observed DESC LIMIT 30')
        metadata = rows(c, 'SELECT * FROM metadata ORDER BY observed DESC LIMIT 1')
    disk = shutil.disk_usage(config.audio)
    services = {name: read_json(config.data / f'{name}.json', {'state': 'starting'}) for name in ('recorder','worker','metadata')}
    for value in services.values():
        value['stale'] = now - value.get('updated', 0) > 90
    alerts = []
    if disk.free < config.min_free_bytes:
        alerts.append('Disk reserve reached. Capture pauses rather than deleting protected recordings.')
    for name, value in services.items():
        if value['stale']:
            alerts.append(f'{name.capitalize()} heartbeat is missing or stale.')
        elif value.get('error'):
            alerts.append(f'{name.capitalize()}: {value["error"]}')
    if totals['oldest_pending'] and now - totals['oldest_pending'] > 3600:
        alerts.append('Some audio has been waiting for recognition for over an hour. It remains preserved.')
    if config.recognition == 'none':
        alerts.append('Audio recognition is disabled. Metadata is logged, and all audio is retained.')
    return dict(now=now, totals=totals, disk=dict(total=disk.total,free=disk.free,used=disk.used),
                services=services, latest=dict(latest) if latest else None, metadata=metadata[0] if metadata else None,
                timeline=timeline, events=events, alerts=alerts,
                settings=dict(provider=config.recognition,chunk_seconds=config.chunk_seconds,
                    auto_delete=config.auto_delete,retention_hours=config.retention_hours,
                    request_interval=config.request_interval, min_free_bytes=config.min_free_bytes))


class Handler(BaseHTTPRequestHandler):
    server_version = 'Airlog/1.0'

    @property
    def config(self):
        return self.server.config

    def log_message(self, fmt, *args):
        log.info('%s %s', self.address_string(), fmt % args)

    def authorized(self):
        password = os.getenv('WEB_PASSWORD', '')
        if not password:
            return True
        expected = base64.b64encode((os.getenv('WEB_USERNAME', 'admin') + ':' + password).encode()).decode()
        if hmac.compare_digest(self.headers.get('Authorization', ''), 'Basic ' + expected):
            return True
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Airlog", charset="UTF-8"')
        self.send_header('Content-Length', '0')
        self.end_headers()
        return False

    def send(self, payload, code=200, content_type='application/json', headers=None):
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode()
        elif isinstance(payload, str):
            payload = payload.encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'same-origin')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; media-src 'self'; img-src 'self' data: https:; frame-ancestors 'none'")
        for key,value in (headers or {}).items():
            self.send_header(key,value)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(payload)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        try:
            self.get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError):
            self.send({'error': 'Invalid request'}, 400)
        except Exception:
            log.exception('HTTP request failed')
            self.send({'error': 'Backend unavailable; see container logs'}, 503)

    def get(self):
        parsed = urlsplit(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        if path == '/healthz':
            return self.send({'status': 'ok'})
        if not self.authorized():
            return
        if path == '/api/status':
            return self.send(snapshot(self.config))
        if path == '/api/chunks':
            where, args = ['1=1'], []
            state = query.get('state', [''])[0]
            if state == 'preserved':
                where.append("deleted_at IS NULL AND (state!='matched' OR review='keep')")
            elif state == 'kept':
                where.append("review='keep'")
            elif state:
                where.append('state=?')
                args.append(state)
            search = query.get('q', [''])[0][:200]
            if search:
                where.append("(COALESCE(artist,'') LIKE ? OR COALESCE(title,'') LIKE ?)")
                args.extend(['%' + search + '%'] * 2)
            limit = min(100, max(1, int(query.get('limit', ['50'])[0])))
            offset = max(0, int(query.get('offset', ['0'])[0]))
            clause = ' AND '.join(where)
            with db(self.config) as c:
                total = c.execute('SELECT COUNT(*) FROM chunks WHERE ' + clause, args).fetchone()[0]
                items = rows(c, 'SELECT * FROM chunks WHERE ' + clause + ' ORDER BY started DESC LIMIT ? OFFSET ?', args + [limit,offset])
            return self.send(dict(items=items,total=total,offset=offset,limit=limit))
        if path == '/api/plays':
            since = float(query.get('since', [str(time.time()-7*86400)])[0])
            offset = max(0,int(query.get('offset',['0'])[0]))
            with db(self.config) as c:
                items = play_runs(c,since,100,offset)
            return self.send(dict(items=items,offset=offset))
        if path == '/api/metadata':
            with db(self.config) as c:
                items = rows(c, '''WITH history AS (
                    SELECT *,LAG(raw) OVER (ORDER BY observed,id) AS previous FROM metadata
                    WHERE observed>?) SELECT * FROM history WHERE raw != previous OR previous IS NULL
                    ORDER BY observed DESC LIMIT 200''', (time.time()-7*86400,))
            return self.send(dict(items=items))
        if path == '/api/export.csv':
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(['chunk_id','observed_start_utc','duration_seconds','state','artist','title','track_key','review','deleted_at'])
            import datetime
            with db(self.config) as c:
                for row in c.execute('SELECT * FROM chunks ORDER BY started'):
                    def safe(value):
                        text = str(value or '')
                        return "'" + text if text.startswith(('=','+','-','@','\t','\r')) else text
                    writer.writerow([row['id'], datetime.datetime.fromtimestamp(row['started'],datetime.timezone.utc).isoformat(),
                        row['duration'],row['state'],safe(row['artist']),safe(row['title']),row['track_key'],row['review'],row['deleted_at']])
            return self.send(output.getvalue(), content_type='text/csv; charset=utf-8',
                             headers={'Content-Disposition':'attachment; filename="airlog-observations.csv"'})
        match = re.fullmatch(r'/api/chunks/([a-f0-9]{32})/(audio|evidence|next)',path)
        if match:
            chunk_id, action = match.groups()
            with db(self.config) as c:
                row = c.execute('SELECT * FROM chunks WHERE id=?',(chunk_id,)).fetchone()
                evidence = rows(c,'SELECT * FROM observations WHERE chunk_id=? ORDER BY observed DESC LIMIT 10',(chunk_id,)) if action == 'evidence' else []
            if not row:
                return self.send({'error':'Not found'},404)
            if action == 'next':
                with db(self.config) as c:
                    following=c.execute('SELECT * FROM chunks WHERE started>? ORDER BY started LIMIT 1',(row['started'],)).fetchone()
                continuous=following and following['session']==row['session'] and abs(following['started']-(row['started']+row['duration']))<.5
                available=continuous and not following['deleted_at'] and audio_path(self.config,following['path']).exists()
                return self.send({'next':dict(following) if available else None})
            if action == 'evidence':
                return self.send(dict(chunk=dict(row),observations=evidence))
            if row['deleted_at']:
                return self.send({'error':'Audio was deleted'},410)
            return self.serve_audio(audio_path(self.config,row['path']))
        assets = {'/':'index.html','/app.js':'app.js','/style.css':'style.css','/favicon.svg':'favicon.svg'}
        if path in assets:
            file = STATIC / assets[path]
            payload = file.read_bytes()
            if path == '/':
                page = payload.decode('utf-8').replace('{{STREAM_NAME}}', html.escape(self.config.stream_name))
                payload = page.encode('utf-8')
            return self.send(payload,content_type=mimetypes.guess_type(file.name)[0] or 'application/octet-stream')
        return self.send({'error':'Not found'},404)

    def serve_audio(self, path):
        try:
            f = path.open('rb')
        except FileNotFoundError:
            return self.send({'error':'Audio file unavailable'},404)
        with f:
            size = os.fstat(f.fileno()).st_size
            start, end = 0, size-1
            header = self.headers.get('Range')
            if header:
                match = re.fullmatch(r'bytes=(\d*)-(\d*)',header)
                if not match or not any(match.groups()):
                    return self.send({},416,headers={'Content-Range':f'bytes */{size}'})
                a,b = match.groups()
                if a:
                    start = int(a)
                    end = min(size-1,int(b)) if b else size-1
                else:
                    start = max(0,size-int(b))
                if start> end or start >= size:
                    return self.send({},416,headers={'Content-Range':f'bytes */{size}'})
            self.send_response(206 if header else 200)
            self.send_header('Content-Type','audio/wav')
            self.send_header('Accept-Ranges','bytes')
            self.send_header('Cache-Control','private, no-store')
            self.send_header('Content-Length',str(end-start+1))
            if header:
                self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
            self.end_headers()
            if self.command == 'HEAD':
                return
            f.seek(start)
            remaining = end-start+1
            while remaining:
                data = f.read(min(65536,remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def do_POST(self):
        if not self.authorized():
            return
        # Non-simple header + no CORS keeps cross-origin browser requests out.
        if self.headers.get('X-Airlog-Request') != '1' or self.headers.get('Content-Type') != 'application/json':
            return self.send({'error':'Expected same-origin JSON request'},403)
        try:
            length = int(self.headers.get('Content-Length','0'))
            if not 0 < length < 4096:
                return self.send({'error':'Invalid body length'},400)
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                return self.send({'error':'Expected JSON object'},400)
            path = urlsplit(self.path).path
            if path == '/api/chunks/delete-unidentified':
                return self.send({'ok':True, 'deleted':delete_unidentified(self.config)})
            match = re.fullmatch(r'/api/chunks/([a-f0-9]{32})/(review|retry)',path)
            if not match:
                return self.send({'error':'Not found'},404)
            chunk_id, action = match.groups()
            with db(self.config) as c:
                c.execute('BEGIN IMMEDIATE')
                row = c.execute('SELECT * FROM chunks WHERE id=?',(chunk_id,)).fetchone()
                if not row:
                    return self.send({'error':'Not found'},404)
                if row['deleted_at'] or not audio_path(self.config,row['path']).exists():
                    return self.send({'error':'Audio is no longer available'},409)
                if action == 'review':
                    review = body.get('review')
                    if review not in ('auto','keep','release'):
                        return self.send({'error':'Invalid review decision'},400)
                    c.execute('UPDATE chunks SET review=? WHERE id=?',(review,chunk_id))
                else:
                    c.execute("UPDATE chunks SET state='pending',next_attempt=0,error=NULL,attempts=0 WHERE id=?",(chunk_id,))
            return self.send({'ok':True})
        except (ValueError,TypeError):
            return self.send({'error':'Invalid request'},400)
        except Exception:
            log.exception('Mutation failed')
            return self.send({'error':'Update failed'},503)


def main(config):
    initialize(config)
    server = ThreadingHTTPServer(('0.0.0.0',config.port),Handler)
    server.config = config
    log.info('Airlog dashboard listening on port %s', config.port)
    server.serve_forever()
