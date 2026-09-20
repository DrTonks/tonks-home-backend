"""Exercise old -> new -> old code against ONE temporary SQLite database.
This never touches production or switches the real PM2 service.
"""
import argparse,importlib,json,os,sqlite3,subprocess,sys,tempfile
from pathlib import Path
from contextlib import closing
ROOT=Path(__file__).resolve().parents[1]

def worker(code, database, action):
    sys.path.insert(0,str(code))
    module=importlib.import_module('community' if (code/'community.py').exists() else 'sleepy_app.community.store')
    store=module.CommunityStore(str(database))
    if action!='read':
        payload=module.validate_comment_payload('about',{'nickname':'Fixture','email':'fixture@example.test','content':action})
        store.create_comment(payload,action,status='published',is_admin=True)
    print(json.dumps([c['content'] for c in store.list_public_comments('about')]))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--baseline',type=Path);p.add_argument('--worker',type=Path);p.add_argument('--database',type=Path);p.add_argument('--action');a=p.parse_args()
    if a.worker:worker(a.worker.resolve(),a.database.resolve(),a.action)
    else:
        if not a.baseline:p.error('--baseline is required')
        with tempfile.TemporaryDirectory(prefix='sleepy-rollback-') as directory:
            db=Path(directory)/'community.sqlite3'
            for code,action,expected in [(a.baseline,'old-write',{'old-write'}),(ROOT,'new-write',{'old-write','new-write'}),(a.baseline,'read',{'old-write','new-write'})]:
                run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--worker',str(code.resolve()),'--database',str(db),'--action',action],capture_output=True,text=True,encoding='utf-8',env={**os.environ,'PYTHONIOENCODING':'utf-8'},timeout=30)
                if run.returncode:raise RuntimeError(run.stderr)
                if set(json.loads(run.stdout))!=expected:raise RuntimeError('Rollback lost fixture comments')
            with closing(sqlite3.connect(db)) as source,closing(sqlite3.connect(Path(directory)/'backup.sqlite3')) as target:
                source.backup(target)
                assert target.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            print('PASS: old -> new -> old retains both writes; SQLite online backup integrity OK')
