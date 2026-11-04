import base64
import contextlib
import io
import http.client
from types import SimpleNamespace
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import uuid
import wave
from unittest.mock import patch

from radio.config import Config
from radio.recorder import Recorder, finish_capture
from radio.recognize import normalize
from radio.storage import atomic_json, audio_path, db, delete_audio, delete_unidentified, ingest, initialize
from radio.worker import parse_metadata, retain, recover_deletions, save_result
from radio.web import Handler, ThreadingHTTPServer, play_runs, snapshot


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.config=Config(data=root/'data',audio=root/'audio',min_free_bytes=0,
                           retention_hours=1,auto_delete=False,recognition='none').prepare()
        initialize(self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def capture(self, start=1000, seconds=15, session='session', recovered=False):
        item=dict(id=uuid.uuid4().hex, session=session, started=start)
        path=self.config.audio/'capture'/item['id']
        atomic_json(path.with_suffix('.json'),item)
        path.with_suffix('.pcm').write_bytes(b'\0\0'*int(self.config.sample_rate*seconds))
        return finish_capture(self.config,item,recovered)

    def chunk(self, start=1000, state='matched', offset=0, session='session', review='auto', recovered=False, seconds=15):
        item=self.capture(start,seconds=seconds,session=session,recovered=recovered)
        ingest(self.config)
        with db(self.config) as c:
            c.execute('UPDATE chunks SET state=?,artist=?,title=?,track_key=?,offset=?,review=? WHERE id=?',
                      (state,'Artist','Song','123' if state=='matched' else None,offset,review,item['id']))
            return dict(c.execute('SELECT * FROM chunks WHERE id=?',(item['id'],)).fetchone())


class DurabilityTests(Base):
    def test_capture_survives_without_database(self):
        (self.config.data/'radio.sqlite3').unlink()
        item=self.capture(seconds=2.25,recovered=True)
        with wave.open(str(audio_path(self.config,item['path']))) as w:
            self.assertEqual(w.getnframes(),36000)
        initialize(self.config)
        ingest(self.config)
        ingest(self.config,rebuild=True)
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],1)
            row=c.execute('SELECT * FROM chunks').fetchone()
            self.assertEqual(row['recovered'],1)
            self.assertEqual(row['duration'],2.25)
        self.assertEqual(list((self.config.audio/'queue').glob('*.json')),[])

    def test_interrupted_odd_pcm_recovered(self):
        item=dict(id=uuid.uuid4().hex,session='old',started=1000)
        path=self.config.audio/'capture'/item['id']
        atomic_json(path.with_suffix('.json'),item)
        path.with_suffix('.pcm').write_bytes(b'\0'*32001)
        result=finish_capture(self.config,item,True)
        self.assertEqual(result['duration'],1)
        self.assertFalse(path.with_suffix('.pcm').exists())

    def test_queue_not_acknowledged_when_database_fails(self):
        self.capture()
        with patch('radio.storage.db',side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                ingest(self.config)
        self.assertEqual(len(list((self.config.audio/'queue').glob('*.json'))),1)
        ingest(self.config)
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM chunks').fetchone()[0],1)

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            audio_path(self.config,'../../secret')
        (self.config.audio/'link').symlink_to(self.config.data,target_is_directory=True)
        with self.assertRaises(ValueError):
            audio_path(self.config,'link/radio.sqlite3')

    def test_real_ffmpeg_finalizes_last_partial_chunk(self):
        fixture=Path(self.tmp.name)/'source.wav'
        with wave.open(str(fixture),'wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(b'\0\0'*16000*32)
        self.config.stream=str(fixture)
        recorder=Recorder(self.config)
        with self.assertRaisesRegex(RuntimeError,'disconnected'):
            recorder.capture()
        ingest(self.config)
        with db(self.config) as c:
            durations=[r[0] for r in c.execute('SELECT duration FROM chunks ORDER BY started')]
        self.assertEqual(durations,[15,15,2])
        self.assertIsNone(recorder.process)


class RecognitionTests(Base):
    def test_normalization_and_no_match(self):
        self.assertEqual(normalize({})['state'],'unknown')
        r=normalize({'track':{'key':'12','title':'Test','subtitle':'Artist'},'matches':[{'offset':45.5}]})
        self.assertEqual(r['offset'],45.5)
        self.assertEqual(r['artist'],'Artist')

    def test_error_is_retryable_and_audio_is_preserved(self):
        item=self.chunk(state='pending')
        save_result(self.config,item,{'state':'error','error':'network down'},now=2000)
        with db(self.config) as c:
            row=c.execute('SELECT * FROM chunks').fetchone()
            self.assertEqual(row['state'],'error')
            self.assertGreater(row['next_attempt'],2000)
            self.assertEqual(row['attempts'],1)
        self.assertEqual(retain(self.config,now=100000),0)
        self.assertTrue(audio_path(self.config,item['path']).exists())

    def test_no_match_gets_one_retry_then_unknown(self):
        item=self.chunk(state='pending')
        save_result(self.config,item,{'state':'unknown'},now=2000)
        with db(self.config) as c:
            item=dict(c.execute('SELECT * FROM chunks').fetchone())
        self.assertEqual(item['state'],'retry')
        save_result(self.config,item,{'state':'unknown'},now=3000)
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT state FROM chunks').fetchone()[0],'unknown')
            self.assertEqual(c.execute('SELECT COUNT(*) FROM observations').fetchone()[0],2)

    def test_metadata_parsing(self):
        for value in [{'artist':'Artist','title':'Song'},{'title':'Artist - Song'},
                      {'now_playing':{'song':{'artist':'Artist','title':'Song'}}},'Artist - Song']:
            self.assertEqual(parse_metadata(value),('Artist','Song'))
        self.assertEqual(parse_metadata({'unexpected':'payload'}),('',''))


class RetentionTests(Base):
    def sequence(self):
        return [self.chunk(start=1000+i*15,offset=30+i*15) for i in range(5)]

    def test_disabled_cleanup_preserves_matched_audio(self):
        self.sequence()
        self.assertEqual(retain(self.config,now=100000),0)

    def test_enabled_cleanup_deletes_all_aged_matches_including_boundaries(self):
        items=self.sequence()
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),5)
        for item in items:
            self.assertFalse(audio_path(self.config,item['path']).exists())
        self.assertEqual(retain(self.config,now=100000),0)
        with db(self.config) as c:
            events=c.execute('SELECT detail FROM events').fetchall()
            self.assertEqual(len(events),5)
            self.assertTrue(all(json.loads(event[0])['reason']=='automatic_matched' for event in events))

    def test_keep_protects_only_the_kept_chunk(self):
        items=self.sequence()
        with db(self.config) as c:
            c.execute("UPDATE chunks SET review='keep' WHERE id=?",(items[2]['id'],))
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),4)
        for i,item in enumerate(items):
            self.assertEqual(audio_path(self.config,item['path']).exists(),i==2)

    def test_discontinuous_offsets_do_not_prevent_deletion(self):
        items=self.sequence()
        with db(self.config) as c:
            c.execute('UPDATE chunks SET offset=999 WHERE id=?',(items[2]['id'],))
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),5)

    def test_session_boundary_does_not_prevent_deletion(self):
        items=self.sequence()
        with db(self.config) as c:
            c.execute("UPDATE chunks SET session='new' WHERE id=?",(items[4]['id'],))
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),5)

    def test_alternating_matches_and_unknown_neighbors_do_not_prevent_deletion(self):
        self.chunk(start=985,state='unknown')
        items=self.sequence()
        self.chunk(start=1075,state='unknown')
        with db(self.config) as c:
            for item in items[::2]:
                c.execute("UPDATE chunks SET track_key='other' WHERE id=?",(item['id'],))
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),5)
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM chunks WHERE deleted_at IS NULL').fetchone()[0],2)

    def test_isolated_recovered_partial_match_without_offset_is_deleted(self):
        item=self.chunk(recovered=True,seconds=2.25,offset=None)
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),1)
        self.assertFalse(audio_path(self.config,item['path']).exists())

    def test_matched_cleanup_waits_one_hour_after_capture_ends(self):
        item=self.chunk(start=1000)
        self.config.auto_delete=True
        deadline=item['started']+item['duration']+3600
        self.assertEqual(retain(self.config,now=deadline-.01),0)
        self.assertTrue(audio_path(self.config,item['path']).exists())
        self.assertEqual(retain(self.config,now=deadline),1)

    def test_matched_cleanup_respects_configured_grace_period(self):
        item=self.chunk(start=1000)
        self.config.auto_delete=True
        self.config.retention_hours=2
        self.assertEqual(retain(self.config,now=4615),0)
        self.assertTrue(audio_path(self.config,item['path']).exists())
        self.assertEqual(retain(self.config,now=8215),1)

    def test_automatic_cleanup_retains_history_and_recognition_evidence(self):
        item=self.chunk()
        with db(self.config) as c:
            c.execute('INSERT INTO observations(chunk_id,observed,provider,result) VALUES (?,?,?,?)',
                      (item['id'],1000,'shazam','{}'))
            before=play_runs(c,0)
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),1)
        with db(self.config) as c:
            row=c.execute('SELECT * FROM chunks').fetchone()
            self.assertEqual(row['state'],'matched')
            self.assertEqual(row['title'],'Song')
            self.assertEqual(row['deleted_at'],100000)
            self.assertEqual(c.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
            self.assertEqual(play_runs(c,0),[{**before[0],'available':0}])

    def test_manual_release_respects_grace_and_retains_history(self):
        item=self.chunk(review='release')
        self.assertEqual(retain(self.config,now=2000),0)
        self.assertEqual(retain(self.config,now=100000),1)
        with db(self.config) as c:
            row=c.execute('SELECT * FROM chunks').fetchone()
            self.assertEqual(row['title'],'Song')
            self.assertEqual(row['deleted_at'],100000)

    def test_recover_deletion_after_crash(self):
        item=self.chunk(review='release')
        atomic_json(self.config.audio/'queue'/(item['id']+'.deleted'),
                    {'id':item['id'],'path':item['path'],'deleted_at':5000})
        recover_deletions(self.config)
        self.assertFalse(audio_path(self.config,item['path']).exists())
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT deleted_at FROM chunks').fetchone()[0],5000)

    def test_keep_wins_over_uncompleted_deletion_intent(self):
        item=self.chunk(review='keep')
        atomic_json(self.config.audio/'queue'/(item['id']+'.deleted'),
                    {'id':item['id'],'path':item['path'],'deleted_at':5000})
        recover_deletions(self.config)
        self.assertTrue(audio_path(self.config,item['path']).exists())
        with db(self.config) as c:
            self.assertIsNone(c.execute('SELECT deleted_at FROM chunks').fetchone()[0])

    def test_unidentified_pending_retry_and_error_audio_are_retained(self):
        items=[self.chunk(state=state) for state in ('unknown','pending','retry','error')]
        items.append(self.chunk(state='unknown',recovered=True,seconds=2.25))
        # Stale song metadata on a pending retry must not count as an active match.
        with db(self.config) as c:
            c.execute("UPDATE chunks SET track_key='123'")
        self.config.auto_delete=True
        self.assertEqual(retain(self.config,now=100000),0)
        for item in items:
            self.assertTrue(audio_path(self.config,item['path']).exists())


class HistoryTests(Base):
    def test_unknown_splits_runs_but_continuous_sessions_do_not(self):
        self.chunk(start=1000)
        self.chunk(start=1015)
        self.chunk(start=1030,state='unknown')
        self.chunk(start=1045)
        self.chunk(start=1060,session='new')
        with db(self.config) as c:
            result=play_runs(c,0)
        self.assertEqual(len(result),2)
        self.assertEqual(result[-1]['samples'],2)
        self.assertEqual(result[0]['samples'],2)
        delete_unidentified(self.config)
        with db(self.config) as c:
            self.assertEqual(play_runs(c,0),result)

    def test_pending_retry_with_old_key_does_not_bridge_runs(self):
        self.chunk(start=1000)
        middle=self.chunk(start=1015)
        self.chunk(start=1030)
        with db(self.config) as c:
            c.execute("UPDATE chunks SET state='pending' WHERE id=?",(middle['id'],))
            self.assertEqual(len(play_runs(c,0)),2)

    def test_empty_snapshot(self):
        data=snapshot(self.config)
        self.assertEqual(data['totals']['captured_today'],0)
        self.assertIsNone(data['latest'])
        self.assertTrue(data['services']['recorder']['stale'])


class WebTests(Base):
    def request(self,path,body=None,headers=None):
        headers=dict(headers or {})
        payload=json.dumps(body).encode() if body is not None else b''
        method='POST' if body is not None else 'GET'
        if body is not None:
            headers={'Content-Type':'application/json','X-Airlog-Request':'1',**headers}
        headers['Host']='localhost'
        headers['Content-Length']=str(len(payload))
        raw=(method+' '+path+' HTTP/1.0\r\n'+''.join(f'{k}: {v}\r\n' for k,v in headers.items())+'\r\n').encode()+payload
        class MemorySocket:
            def __init__(self,data):
                self.input=io.BytesIO(data)
                self.output=io.BytesIO()
            def makefile(self,*args,**kwargs):
                return self.input
            def sendall(self,data):
                self.output.write(data)
        sock=MemorySocket(raw)
        Handler(sock,('127.0.0.1',12345),SimpleNamespace(config=self.config))
        response=http.client.HTTPResponse(MemorySocket(sock.output.getvalue()))
        response.begin()
        if response.status>=400:
            raise urllib.error.HTTPError(path,response.status,response.reason,response.headers,response)
        return response

    def test_status_and_static(self):
        with self.request('/') as response:
            self.assertIn(b'Every moment, accounted for',response.read())
            self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        with self.request('/api/status') as response:
            self.assertEqual(json.load(response)['totals']['chunks'],0)

    def test_audio_range_and_suffix(self):
        item=self.chunk()
        path='/api/chunks/'+item['id']+'/audio'
        with self.request(path,headers={'Range':'bytes=0-43'}) as response:
            self.assertEqual(response.status,206)
            self.assertEqual(len(response.read()),44)
        with self.request(path,headers={'Range':'bytes=-12'}) as response:
            self.assertEqual(len(response.read()),12)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(path,headers={'Range':'bytes=999999999-'})
        self.assertEqual(error.exception.code,416)

    def test_continuous_playback_stops_at_connection_boundary(self):
        first=self.chunk(start=1000)
        second=self.chunk(start=1015)
        self.chunk(start=1030,session='another-session')
        with self.request('/api/chunks/'+first['id']+'/next') as response:
            self.assertEqual(json.load(response)['next']['id'],second['id'])
        with self.request('/api/chunks/'+second['id']+'/next') as response:
            self.assertIsNone(json.load(response)['next'])

    def test_deleted_audio_returns_gone(self):
        item=self.chunk(review='release')
        retain(self.config,now=100000)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/chunks/'+item['id']+'/audio')
        self.assertEqual(error.exception.code,410)

    def test_keep_release_retry(self):
        item=self.chunk(state='unknown')
        for review in ('keep','release','auto'):
            with self.request('/api/chunks/'+item['id']+'/review',{'review':review}) as response:
                self.assertTrue(json.load(response)['ok'])
        with self.request('/api/chunks/'+item['id']+'/retry',{}) as response:
            self.assertTrue(json.load(response)['ok'])
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT state FROM chunks').fetchone()[0],'pending')

    def test_bulk_delete_only_unkept_unidentified_audio(self):
        targets=[self.chunk(state='unknown'),
                 self.chunk(start=time.time(),state='unknown',review='release'),
                 self.chunk(state='unknown',recovered=True)]
        protected=[self.chunk(state='unknown',review='keep')]
        protected += [self.chunk(state=state) for state in ('matched','pending','retry','error','missing')]
        with db(self.config) as c:
            c.execute('INSERT INTO observations(chunk_id,observed,provider,result) VALUES (?,?,?,?)',
                      (targets[0]['id'],1000,'shazam','{}'))
        # Global scope: URL filters and pagination do not restrict deletion.
        with self.request('/api/chunks/delete-unidentified?state=matched&q=absent&limit=1&offset=50',{}) as response:
            self.assertEqual(json.load(response),{'ok':True,'deleted':3})
        for item in targets:
            self.assertFalse(audio_path(self.config,item['path']).exists())
        for item in protected:
            self.assertTrue(audio_path(self.config,item['path']).exists())
        with db(self.config) as c:
            for item in targets + protected:
                row=c.execute('SELECT * FROM chunks WHERE id=?',(item['id'],)).fetchone()
                self.assertEqual(row['deleted_at'] is not None,item in targets)
                self.assertEqual(row['state'],item['state'])
                self.assertEqual(row['review'],item['review'])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM observations').fetchone()[0],1)
            events=c.execute("SELECT detail FROM events WHERE kind='audio_deleted'").fetchall()
            self.assertEqual(len(events),3)
            self.assertTrue(all(json.loads(event[0])['reason']=='manual_unidentified' for event in events))
        self.assertEqual(list((self.config.audio/'queue').glob('*.deleted')),[])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/chunks/'+targets[0]['id']+'/audio')
        self.assertEqual(error.exception.code,410)

    def test_bulk_delete_all_pages_and_repeat_safely(self):
        for i in range(51):
            self.chunk(start=1000+i*15,state='unknown')
        with self.request('/api/chunks/delete-unidentified',{}) as response:
            self.assertEqual(json.load(response)['deleted'],51)
        with self.request('/api/chunks/delete-unidentified',{}) as response:
            self.assertEqual(json.load(response)['deleted'],0)
        with db(self.config) as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],51)

    def test_bulk_delete_missing_file_and_empty_library(self):
        with self.request('/api/chunks/delete-unidentified',{}) as response:
            self.assertEqual(json.load(response)['deleted'],0)
        item=self.chunk(state='unknown')
        audio_path(self.config,item['path']).unlink()
        with self.request('/api/chunks/delete-unidentified',{}) as response:
            self.assertEqual(json.load(response)['deleted'],1)

    def test_bulk_delete_recovers_after_interrupted_transaction(self):
        item=self.chunk(state='unknown')
        def interrupted(*args):
            delete_audio(*args)
            raise RuntimeError('simulated crash before commit')
        with patch('radio.storage.delete_audio',side_effect=interrupted):
            with self.assertLogs('radio.web',level='ERROR'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request('/api/chunks/delete-unidentified',{})
        self.assertEqual(error.exception.code,503)
        self.assertFalse(audio_path(self.config,item['path']).exists())
        with db(self.config) as c:
            self.assertIsNone(c.execute('SELECT deleted_at FROM chunks').fetchone()[0])
        self.assertTrue((self.config.audio/'queue'/(item['id']+'.deleted')).exists())
        recover_deletions(self.config)
        with db(self.config) as c:
            self.assertIsNotNone(c.execute('SELECT deleted_at FROM chunks').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],1)
        self.assertEqual(list((self.config.audio/'queue').glob('*.deleted')),[])

    def test_bulk_delete_preserves_audio_if_deletion_intent_cannot_be_written(self):
        item=self.chunk(state='unknown')
        with patch('radio.storage.atomic_json',side_effect=OSError('disk full')):
            with self.assertLogs('radio.web',level='ERROR'):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request('/api/chunks/delete-unidentified',{})
        self.assertEqual(error.exception.code,503)
        self.assertTrue(audio_path(self.config,item['path']).exists())
        with db(self.config) as c:
            self.assertIsNone(c.execute('SELECT deleted_at FROM chunks').fetchone()[0])
            self.assertEqual(c.execute('SELECT COUNT(*) FROM events').fetchone()[0],0)

    def test_bulk_delete_requires_authentication_and_same_origin_json(self):
        item=self.chunk(state='unknown')
        for headers,code in [({'X-Airlog-Request':''},403),({'Content-Type':'text/plain'},403)]:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/chunks/delete-unidentified',{},headers=headers)
            self.assertEqual(error.exception.code,code)
        with patch.dict(os.environ,{'WEB_PASSWORD':'test-pass'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/chunks/delete-unidentified',{})
            self.assertEqual(error.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/chunks/delete-unidentified',[])
        self.assertEqual(error.exception.code,400)
        self.assertTrue(audio_path(self.config,item['path']).exists())

    def test_basic_auth_and_healthcheck(self):
        with patch.dict(os.environ,{'WEB_PASSWORD':'test-pass','WEB_USERNAME':'admin'}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request('/api/status')
            self.assertEqual(error.exception.code,401)
            token=base64.b64encode(b'admin:test-pass').decode()
            with self.request('/api/status',headers={'Authorization':'Basic '+token}) as response:
                self.assertEqual(response.status,200)
            with self.request('/healthz') as response:
                self.assertEqual(response.status,200)

    def test_reject_cross_origin_simple_request(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request('/api/chunks/'+uuid.uuid4().hex+'/review',{},headers={'X-Airlog-Request':''})
        self.assertEqual(error.exception.code,403)

    def test_search_and_csv_sanitization(self):
        item=self.chunk()
        with db(self.config) as c:
            c.execute("UPDATE chunks SET title='=HYPERLINK(danger)' WHERE id=?",(item['id'],))
        with self.request('/api/chunks?q=HYPERLINK') as response:
            self.assertEqual(json.load(response)['total'],1)
        with self.request('/api/export.csv') as response:
            self.assertIn(b"'=HYPERLINK",response.read())

if __name__ == '__main__':
    unittest.main()
