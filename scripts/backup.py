"""Online SQLite backup. Run inside web container: python scripts/backup.py"""
import datetime
import os
from pathlib import Path
import sqlite3

folder=Path(os.getenv('DATA_DIR','/data'))
backups=folder/'backups'
backups.mkdir(exist_ok=True)
target=backups/(datetime.datetime.now(datetime.timezone.utc).strftime('airlog-%Y%m%dT%H%M%SZ')+'.sqlite3')
with sqlite3.connect(folder/'radio.sqlite3') as source, sqlite3.connect(target) as dest:
    source.backup(dest)
print(target)
