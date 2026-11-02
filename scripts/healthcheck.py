import json
import os
from pathlib import Path
import sys
import time
import urllib.request

role = sys.argv[1]
try:
    if role == 'web':
        with urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3) as r:
            assert r.status == 200
    else:
        value=json.loads((Path(os.getenv('DATA_DIR','/data')) / f'{role}.json').read_text())
        assert time.time()-value['updated'] < 90
        assert value['state'] not in ('stopped','error')
        if role == 'recorder':
            assert value['state'] == 'recording' and time.time()-value.get('last_audio',0)<45
except Exception:
    sys.exit(1)
