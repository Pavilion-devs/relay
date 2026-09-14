"""Isolated Linux Compose checks; synthetic volumes are removed on completion."""
import argparse
import json
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen
from uuid import uuid4


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image',required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Preserve earlier reports')
    checks={}
    with socket.socket() as s:
        s.bind(('127.0.0.1',0)); port=s.getsockname()[1]
    project='relay-package-'+uuid4().hex[:8]
    with tempfile.TemporaryDirectory(prefix='relay-package-') as directory:
        envfile=Path(directory)/'compose.env'
        envfile.write_text(f'RELAY_IMAGE={args.image}\nRELAY_VOLUME_ID={project}\nRELAY_PORT={port}\nRELAY_REQUIRE_REMOTE_BACKUP=0\n')
        base=['docker','compose','--env-file',str(envfile),'-f','deploy/compose.yaml','-p',project]
        def command(*parts,expect=0):
            result=subprocess.run(base+list(parts),capture_output=True,text=True,timeout=120,check=False)
            if expect is not None and result.returncode!=expect:
                raise RuntimeError(f'Compose {parts[0]} failed: {result.stderr[-1500:]}')
            return result
        def ready():
            try:
                with urlopen(f'http://127.0.0.1:{port}/ready',timeout=4) as response:
                    return response.status==200
            except (HTTPError,URLError,OSError): return False
        def wait_ready():
            for _ in range(60):
                if ready(): return
                time.sleep(1)
            logs=command('logs','--tail','30',expect=None).stdout[-3000:]
            raise RuntimeError('Readiness did not recover: '+logs)
        try:
            checks['uninitialized_volume_rejected']=command('run','--rm','tools','ready',expect=None).returncode!=0
            command('run','--rm','tools','init')
            command('up','-d','api','worker','maintenance')
            wait_ready(); checks['initial_readiness']=True
            checks['non_root_linux_amd64']=command('exec','-T','api','python','-c','import os,platform; assert os.getuid()==10001; assert platform.machine()=="x86_64"').returncode==0
            command('exec','-T','api','python','-c','from pathlib import Path; import os; assert not any(Path(p).exists() for p in ("/app/.env","/app/.data","/app/web")); assert not any(os.environ.get(k) for k in ("AWS_ACCESS_KEY_ID","AWS_SECRET_ACCESS_KEY","AWS_SESSION_TOKEN","AWS_PROFILE"))')
            checks['private_artifacts_and_aws_credentials_absent']=True
            # Seed only synthetic engine data, never import the user's database.
            seeded=command('exec','-T','api','python','-c',
                'from relay_core.store import Store; from relay_core.engine import seed; import os; s=Store(os.environ["RELAY_DB"]).create(seed()); print(s["id"])').stdout.strip()
            command('restart','api'); wait_ready()
            command('exec','-T','api','python','-c',
                f'from relay_core.store import Store; import os; assert Store(os.environ["RELAY_DB"]).read({seeded!r})["total_kg"]==320')
            checks['api_restart_preserves_database']=True
            command('stop','worker')
            # Set the stored timestamp stale, avoiding a long sleep in the drill.
            command('exec','-T','api','python','-c',
                'import sqlite3,os; c=sqlite3.connect(os.environ["RELAY_DB"]); c.execute("UPDATE service_health SET completed=0 WHERE name=\'worker\'"); c.commit()')
            checks['stopped_worker_fails_readiness']=not ready()
            command('start','worker'); wait_ready(); checks['worker_restart_recovers']=True
            manifest=json.loads(command('run','--rm','tools','backup').stdout.strip())
            source='/var/backups/relay/'+manifest['file']
            checks['restore_while_running_rejected']=command('run','--rm','tools','restore','--source',source,expect=None).returncode!=0
            command('stop','worker','maintenance')
            checks['api_alone_blocks_restore']=command('run','--rm','tools','restore','--source',source,expect=None).returncode!=0
            command('stop','api')
            command('run','--rm','tools','restore','--source',source)
            checks['restored_volume_quarantined']=command('run','--rm','tools','ready',expect=None).returncode!=0
            checks['api_start_on_restore_rejected']=command('run','--rm','--no-deps','tools','api',expect=None).returncode!=0
            review={'backup_sha256':manifest['sha256'],'reconciled':True,'reviewer':'synthetic-drill','notes':'Only synthetic local state; no external side effects exist.'}
            code='from pathlib import Path; from relay_core.operations import release; p=Path("/tmp/review.json"); p.write_text('+repr(json.dumps(review))+'); release(p)'
            command('run','--rm','--entrypoint','python','tools','-c',code)
            command('start','api','worker','maintenance'); wait_ready()
            checks['explicit_review_allows_restart']=True
            command('exec','-T','api','python','-c',
                f'from relay_core.store import Store; import os; assert Store(os.environ["RELAY_DB"]).read({seeded!r})["total_kg"]==320')
            checks['restored_workspace_survives']=True
            assert all(checks.values()),checks
        finally:
            command('down','--volumes','--remove-orphans')
        metadata=json.loads(subprocess.check_output(['docker','image','inspect',args.image],text=True))[0]
        report={'checks':checks,'image_id':metadata['Id'],'architecture':metadata['Architecture'],
                'scope':'Isolated local Linux amd64 containers; synthetic data; no AWS credentials, cloud writes, email, or Bedrock calls. Off-host backup disabled only for this drill.',
                'synthetic_resources_removed':True}
        with args.output.open('x') as f:
            json.dump(report,f,indent=2); f.write('\n')
        print(json.dumps(report))


if __name__=='__main__': main()
