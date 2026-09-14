"""Synthetic SQLite online-backup/restore drill; never reads the user's live database."""
import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from relay_core import execution
from relay_core.api import create_app
from scripts.rehearse_commitment_failure import Rehearsal


def rows(db):
    names=[r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    return {name: sorted(db.execute('SELECT * FROM "'+name+'"').fetchall(),key=repr) for name in names}


def run(directory):
    source=directory/'source.sqlite3'
    restored=directory/'restored.sqlite3'
    r=Rehearsal(source)
    checks={}
    try:
        r.command('coordinator','propose')
        for rid in r.read()['plan']['required']:
            r.command(rid,'accept',resource_id=rid,revision=1)
        r.command('coordinator','approve',revision=1,key='original-approval')
        r.command('tunde','pickup',resource_id='tunde',revision=1)
        with r.store.connect() as db:
            db.execute('PRAGMA wal_autocheckpoint=0')
            db.execute('BEGIN IMMEDIATE')
            ticket=execution.claim(db,r.clock+3600)
            db.commit()
            assert ticket
            checks['nonempty_wal_at_backup']=Path(str(source)+'-wal').stat().st_size>0
            original=rows(db)
            with sqlite3.connect(restored) as destination:
                db.backup(destination)
                checks['integrity_ok']=destination.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
                checks['all_tables_and_rows_restored']=rows(destination)==original
        # Late writes distinguish a real recovery point from an in-place reopen.
        with r.store.connect() as db:
            db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?",(ticket['id'],))
        app=create_app(str(restored))
        app.state.store.clock=lambda:r.clock
        with TestClient(app) as client:
            state=app.state.store.read(r.wid)
            checks['custody_preserved']=state['plan']['picked_up']==['tunde']
            checks['reservations_preserved']=app.state.store.reservations(r.network)==r.store.reservations(r.network)
            with app.state.store.connect() as db:
                checks['later_source_write_excluded']=rows(db)==original
            response=client.post(f'/workspaces/{r.wid}/commands',json={
                'action':'approve','revision':1,'command_id':'original-approval',
            },headers={'Authorization':'Bearer '+r.tokens['coordinator']})
            checks['authorized_command_replay_succeeds']=response.status_code==200
            checks['replay_does_not_duplicate_reservations']=app.state.store.reservations(r.network)==r.store.reservations(r.network)
            with app.state.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                recovered=execution.claim(db,r.clock+3661)
                checks['expired_worker_lease_reclaimed']=bool(recovered and recovered['id']==ticket['id'] and recovered['lease']!=ticket['lease'])
                checks['old_worker_fenced']=not execution.deliver(db,ticket,r.clock+3661)
        assert all(checks.values()),checks
        return {'checks':checks,'table_count':len(original),'scope':'Synthetic local online-backup and restored HTTP/lease checks. No live database, cloud snapshot, external sends or disk-loss RTO measurement.','backup_method':'sqlite3.Connection.backup','credential_values_exported':False}
    finally:
        r.client.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists():
        parser.error('Preserve existing evidence')
    with tempfile.TemporaryDirectory(prefix='relay-restore-audit-') as directory:
        result=run(Path(directory))
    result['source_sha256']={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [Path(__file__),Path('relay_core/store.py'),Path('relay_core/execution.py')]}
    with args.output.open('x') as f:
        json.dump(result,f,indent=2)
        f.write('\n')
    print(json.dumps(result['checks']))


if __name__=='__main__':
    main()
