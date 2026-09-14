"""Single-host managed runtime, online backups and offline restore quarantine."""
import argparse
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import sys
import threading
import time
from contextlib import closing, contextmanager
from pathlib import Path


def sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def root():
    return Path(os.environ.get('RELAY_DATA_DIR', '/var/lib/relay'))


def volume(directory=None, allow_quarantine=False):
    directory = directory or root()
    if not directory.is_absolute() or directory.is_symlink() or not os.path.ismount(directory):
        raise RuntimeError('Persistent data mount is missing')
    expected = os.environ.get('RELAY_VOLUME_ID')
    marker = directory / '.relay-volume'
    if not expected or marker.is_symlink() or marker.read_text().strip() != expected:
        raise RuntimeError('Persistent volume identity mismatch')
    if not allow_quarantine and (directory / '.restore-quarantine').exists():
        raise RuntimeError('Restored data is quarantined; reconcile before service startup')
    database = directory / 'relay.sqlite3'
    if database.is_symlink() or not database.is_file():
        raise RuntimeError('Initialized database is missing')
    return database


@contextmanager
def lock(exclusive=False):
    directory = root()
    # The volume must already exist; never create a replacement mount directory.
    with (directory / '.service-lock').open('a') as handle:
        fcntl.flock(handle, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        yield handle


def initialize():
    directory = root()
    if not directory.is_absolute() or directory.is_symlink() or not os.path.ismount(directory):
        raise RuntimeError('Initialize only an explicitly mounted volume')
    identifier = os.environ.get('RELAY_VOLUME_ID', '')
    if not identifier or len(identifier) > 100 or '\n' in identifier:
        raise ValueError('Set a nonempty volume identity')
    if any(p.name != 'lost+found' for p in directory.iterdir()):
        raise RuntimeError('Initialization requires an empty volume')
    from .store import Store
    store = Store(str(directory / 'relay.sqlite3'))
    with closing(store.connect()) as db, db:
        db.execute('CREATE TABLE service_health (name TEXT PRIMARY KEY, completed REAL NOT NULL)')
    with (directory / '.relay-volume').open('x') as f:
        f.write(identifier+'\n')
        f.flush()
        os.fsync(f.fileno())
    sync_directory(directory)


def heartbeat(name):
    with closing(sqlite3.connect(volume().as_uri()+"?mode=rw", uri=True, timeout=3)) as db, db:
        db.execute('INSERT INTO service_health VALUES (?,?) ON CONFLICT(name) DO UPDATE SET completed=excluded.completed', (name,time.time()))


def readiness():
    try:
        database=volume()
        with closing(sqlite3.connect(database.as_uri()+"?mode=rw", uri=True, timeout=2)) as db:
            db.execute('BEGIN IMMEDIATE')
            health=dict(db.execute('SELECT name,completed FROM service_health'))
            db.rollback()
        now=time.time()
        checks={name:0 <= now-health.get(name,0) <= age for name,age in [('worker',30),('maintenance',90),('backup',1800)]}
        return {'ok':all(checks.values()),'checks':checks}
    except (OSError, RuntimeError, sqlite3.Error):
        return {'ok':False,'checks':{'storage':False}}


def backup():
    database=volume()
    directory=Path(os.environ.get('RELAY_BACKUP_DIR','/var/backups/relay'))
    if not directory.is_absolute() or directory.is_symlink() or not os.path.ismount(directory) or directory==root():
        raise RuntimeError('A separate backup mount is required')
    bucket=os.environ.get('RELAY_BACKUP_BUCKET')
    if not bucket and os.environ.get('RELAY_REQUIRE_REMOTE_BACKUP')=='1':
        raise RuntimeError('Off-host backup required but destination is not configured')
    pending=[p for p in directory.glob('relay-*.sqlite3*') if p.suffix=='.partial' or (p.suffix=='.sqlite3' and not Path(str(p)+'.json').exists())]
    if len(pending)>=8:
        raise RuntimeError('Unfinished backups require operator attention before more writes')
    name=f'relay-{time.time_ns()}.sqlite3'
    temporary=directory/(name+'.partial')
    final=directory/name
    with closing(sqlite3.connect(database.as_uri()+"?mode=ro",uri=True,timeout=5)) as src, closing(sqlite3.connect(temporary)) as dst:
        src.backup(dst)
        if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
            raise RuntimeError('Backup integrity check failed')
        dst.execute('PRAGMA journal_mode=DELETE')
    temporary.chmod(0o600)
    with temporary.open('rb') as f:
        os.fsync(f.fileno())
    temporary.rename(final)
    sync_directory(directory)
    checksum=hashlib.sha256(final.read_bytes()).hexdigest()
    manifest={'file':name,'sha256':checksum,'created':time.time(),'volume_id':os.environ['RELAY_VOLUME_ID']}
    if bucket:
        import boto3
        prefix=os.environ.get('RELAY_BACKUP_PREFIX','relay').strip('/')
        key=f'{prefix}/{os.environ["RELAY_VOLUME_ID"]}/{name}'
        client=boto3.client('s3')
        client.upload_file(str(final),bucket,key,ExtraArgs={'ServerSideEncryption':'AES256'})
        client.put_object(Bucket=bucket,Key=key+'.json',Body=json.dumps(manifest).encode(),ServerSideEncryption='AES256')
    elif os.environ.get('RELAY_REQUIRE_REMOTE_BACKUP')=='1':
        raise RuntimeError('Off-host backup required but destination is not configured')
    (directory/(name+'.json')).write_text(json.dumps(manifest)+'\n')
    heartbeat('backup')
    # Only prune complete local backups after a successful new backup/upload.
    complete=sorted(directory.glob('relay-*.sqlite3.json'))
    for old in complete[:-8]:
        old.with_suffix('').unlink(missing_ok=True)
        old.unlink()
    return manifest


def restore(source):
    # Only restore into an initialized, offline volume. Never clear quarantine here.
    volume(allow_quarantine=True)
    with lock(exclusive=True):
        manifest=json.loads(Path(str(source)+'.json').read_text())
        if hashlib.sha256(source.read_bytes()).hexdigest()!=manifest['sha256']:
            raise ValueError('Backup checksum mismatch')
        directory=root()
        target=directory/'restore.partial'
        if target.exists():
            raise RuntimeError('Inspect earlier partial restore before retrying')
        with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as src, closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            if dst.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                raise RuntimeError('Restore integrity check failed')
            dst.execute('PRAGMA journal_mode=DELETE')
        # Fence every service before swapping any database files, even after a crash.
        with (directory/'.restore-quarantine').open('w') as fence:
            fence.write(json.dumps({'backup':manifest,'reason':'Reconcile post-backup custody, commands and external sends before restart.'})+'\n')
            fence.flush()
            os.fsync(fence.fileno())
        sync_directory(directory)
        with closing(sqlite3.connect(target)) as db,db:
            db.execute('DELETE FROM service_health')
        for suffix in ('-wal','-shm'):
            (directory/('relay.sqlite3'+suffix)).unlink(missing_ok=True)
        with target.open('rb') as f:
            os.fsync(f.fileno())
        os.replace(target,directory/'relay.sqlite3')
        sync_directory(directory)


def release(evidence):
    volume(allow_quarantine=True)
    with lock(exclusive=True):
        fence=root()/'.restore-quarantine'
        quarantined=json.loads(fence.read_text())
        review=json.loads(evidence.read_text())
        if (review.get('backup_sha256') != quarantined['backup']['sha256']
                or review.get('reconciled') is not True
                or not isinstance(review.get('reviewer'), str) or not review['reviewer'].strip()
                or not isinstance(review.get('notes'), str) or not review['notes'].strip()):
            raise ValueError('Explicit matching reconciliation evidence is required')
        with closing(sqlite3.connect(volume(allow_quarantine=True))) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS restore_reviews (record TEXT NOT NULL)')
            db.execute('INSERT INTO restore_reviews VALUES (?)', (json.dumps({**review, 'recorded_at':time.time()}),))
        with (root()/'.restore-approvals.jsonl').open('a') as log:
            log.write(json.dumps({**review,'recorded_at':time.time()})+'\n')
            log.flush()
            os.fsync(log.fileno())
        fence.unlink()
        sync_directory(root())


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['init','api','worker','maintenance','backup','restore','release','ready'])
    parser.add_argument('--source',type=Path)
    parser.add_argument('--evidence',type=Path)
    args=parser.parse_args()
    if args.action=='init':
        initialize()
        return
    if args.action=='restore':
        if not args.source or not args.source.is_absolute():
            parser.error('--source must be an absolute backup path')
        restore(args.source)
        return
    if args.action=='release':
        if not args.evidence:
            parser.error('--evidence is required')
        release(args.evidence)
        return
    if args.action=='ready':
        result=readiness()
        print(json.dumps(result))
        raise SystemExit(0 if result['ok'] else 1)
    database=volume()
    with lock() as handle:
        volume()  # Restore may have completed between the first check and lock acquisition.
        if args.action=='api':
            os.environ['RELAY_MANAGED']='1'
            os.environ['RELAY_DB']=str(database)
            os.set_inheritable(handle.fileno(),True)
            os.execv(sys.executable,[sys.executable,'-m','uvicorn','relay_core.api:app','--host','0.0.0.0','--port','8765','--no-access-log','--no-proxy-headers'])
        if args.action=='backup':
            print(json.dumps(backup()))
            return
        from .store import Store
        store=Store(str(database))
        stopping=threading.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            signal.signal(sig,lambda *_:stopping.set())
        next_backup=0
        while not stopping.is_set():
            volume()
            if args.action=='worker':
                store.run_due(limit=10)
            elif time.monotonic()>=next_backup:
                backup()
                next_backup=time.monotonic()+900
            heartbeat(args.action)
            stopping.wait(1 if args.action=='worker' else 30)


if __name__=='__main__':
    main()
