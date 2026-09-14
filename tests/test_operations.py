import json
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient

from relay_core import operations as ops
from relay_core.api import create_app


@pytest.fixture
def managed(tmp_path,monkeypatch):
    data=tmp_path/'data'
    backups=tmp_path/'backups'
    data.mkdir(); backups.mkdir()
    monkeypatch.setenv('RELAY_DATA_DIR',str(data))
    monkeypatch.setenv('RELAY_VOLUME_ID','test-volume')
    monkeypatch.setenv('RELAY_BACKUP_DIR',str(backups))
    monkeypatch.delenv('RELAY_BACKUP_BUCKET',raising=False)
    monkeypatch.delenv('RELAY_REQUIRE_REMOTE_BACKUP',raising=False)
    monkeypatch.setattr(ops.os.path,'ismount',lambda p:p in (data,backups))
    ops.initialize()
    return data,backups


def test_missing_mount_never_initializes(tmp_path,monkeypatch):
    monkeypatch.setenv('RELAY_DATA_DIR',str(tmp_path/'missing'))
    with pytest.raises(RuntimeError): ops.initialize()
    assert not (tmp_path/'missing').exists()


def test_identity_missing_database_and_repeat_init_fail_closed(managed,monkeypatch):
    data,_=managed
    with pytest.raises(RuntimeError): ops.initialize()
    monkeypatch.setenv('RELAY_VOLUME_ID','other-volume')
    with pytest.raises(RuntimeError): ops.volume()
    monkeypatch.setenv('RELAY_VOLUME_ID','test-volume')
    (data/'relay.sqlite3').unlink()
    with pytest.raises(RuntimeError): ops.volume()


def test_readiness_tracks_worker_backup_and_storage(managed):
    assert not ops.readiness()['ok']
    for name in ('worker','maintenance','backup'): ops.heartbeat(name)
    assert ops.readiness()['ok']
    with sqlite3.connect(ops.volume()) as db:
        db.execute("UPDATE service_health SET completed=? WHERE name='worker'",(time.time()-31,))
    assert not ops.readiness()['ok']
    (managed[0]/'.restore-quarantine').write_text('{}')
    assert not ops.readiness()['ok']


def test_restore_requires_offline_lock_and_matching_review(managed,tmp_path):
    data,backups=managed
    manifest=ops.backup()
    source=backups/manifest['file']
    with ops.lock(),pytest.raises(BlockingIOError): ops.restore(source)
    ops.restore(source)
    with pytest.raises(RuntimeError): ops.volume()
    review=tmp_path/'review.json'
    review.write_text(json.dumps({'backup_sha256':'wrong','reconciled':True,'reviewer':'test','notes':'Synthetic reconciliation'}))
    with pytest.raises(ValueError): ops.release(review)
    assert (data/'.restore-quarantine').exists()
    review.write_text(json.dumps({'backup_sha256':manifest['sha256'],'reconciled':True,'reviewer':'test','notes':'Synthetic reconciliation'}))
    ops.release(review)
    assert ops.volume().is_file()
    assert not ops.readiness()['ok']  # Restored health never starts services as ready.
    assert (data/'.restore-approvals.jsonl').is_file()
    with sqlite3.connect(ops.volume()) as db:
        assert json.loads(db.execute('SELECT record FROM restore_reviews').fetchone()[0])['backup_sha256']==manifest['sha256']


def test_tampered_backup_does_not_replace_database(managed):
    _,backups=managed
    manifest=ops.backup()
    source=backups/manifest['file']
    before=ops.volume().read_bytes()
    source.write_bytes(b'tampered')
    with pytest.raises(ValueError): ops.restore(source)
    assert ops.volume().read_bytes()==before


def test_required_remote_backup_never_reports_success_without_bucket(managed,monkeypatch):
    monkeypatch.setenv('RELAY_REQUIRE_REMOTE_BACKUP','1')
    with pytest.raises(RuntimeError): ops.backup()
    with sqlite3.connect(ops.volume()) as db:
        assert db.execute("SELECT * FROM service_health WHERE name='backup'").fetchone() is None


def test_managed_api_refuses_wrong_database_and_observes_quarantine(managed,monkeypatch):
    monkeypatch.setenv('RELAY_MANAGED','1')
    database=ops.volume()
    monkeypatch.setenv('RELAY_DB',str(database))
    with pytest.raises(ValueError): create_app(str(database.parent/'wrong.sqlite3'))
    with pytest.raises(ValueError): create_app(str(database),rehearsal=True)
    with TestClient(create_app(str(database))) as client:
        assert client.get('/ready').status_code==503
        for name in ('worker','maintenance','backup'): ops.heartbeat(name)
        assert client.get('/ready').status_code==200
        (managed[0]/'.restore-quarantine').write_text('{}')
        assert client.get('/ready').status_code==503
        assert client.post('/identity/session',json={}).status_code==503


def test_remote_backup_uses_encryption_and_failure_does_not_refresh_health(managed,monkeypatch):
    import boto3

    calls=[]
    class Client:
        def upload_file(self,*args,**kwargs):
            calls.append(('upload',kwargs))
        def put_object(self,**kwargs):
            calls.append(('manifest',kwargs))
    monkeypatch.setenv('RELAY_REQUIRE_REMOTE_BACKUP','1')
    monkeypatch.setenv('RELAY_BACKUP_BUCKET','synthetic-no-network')
    monkeypatch.setattr(boto3,'client',lambda _:Client())
    ops.backup()
    assert calls[0][1]['ExtraArgs']['ServerSideEncryption']=='AES256'
    assert calls[1][1]['ServerSideEncryption']=='AES256'
    with sqlite3.connect(ops.volume()) as db:
        before=db.execute("SELECT completed FROM service_health WHERE name='backup'").fetchone()[0]
    class Failed(Client):
        def upload_file(self,*args,**kwargs):
            raise RuntimeError('Simulated upload failure')
    monkeypatch.setattr(boto3,'client',lambda _:Failed())
    with pytest.raises(RuntimeError): ops.backup()
    with sqlite3.connect(ops.volume()) as db:
        assert db.execute("SELECT completed FROM service_health WHERE name='backup'").fetchone()[0]==before
