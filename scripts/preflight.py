"""Start the REAL compatibility entry with Waitress and isolated temporary data.
No cloud connection, production data or real status upload is performed.
"""
import json, os, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def main(code_root=ROOT):
    with tempfile.TemporaryDirectory(prefix='sleepy-preflight-') as directory:
        root=Path(directory)
        (root/'data.json').write_text(json.dumps({'host':'127.0.0.1','status_list':[],'status':0}))
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        env={k:v for k,v in os.environ.items() if not k.startswith('SLEEPY_')}
        env.update(SLEEPY_DATA_DIR=directory,SLEEPY_ENV_FILE=str(root/'absent.env'),SLEEPY_HOST='127.0.0.1',SLEEPY_PORT=str(port),PYTHONIOENCODING='utf-8')
        env.update(SLEEPY_DATA_FILE=str(root/'data.json'),SLEEPY_MUSIC_DIR=str(root/'music'),SLEEPY_GITHUB_CACHE_FILE=str(root/'github.json'),SLEEPY_ARTICLE_MANIFEST=str(root/'article-comments-manifest.json'))
        for key in ['ANALYTICS','AGENT_ACTIVITY','RECOMMENDATIONS','COMMUNITY']:env['SLEEPY_'+key+'_DB']=str(root/(key+'.sqlite3'))
        with (root/'server.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([sys.executable,str(code_root/'server.py')],cwd=root,env=env,stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            try:
                for _ in range(100):
                    if process.poll() is not None:raise RuntimeError('Startup failed; '+(root/'server.log').read_text(encoding='utf-8'))
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/get/status_list',timeout=.5) as response:
                            if response.status==200:break
                    except OSError:time.sleep(.1)
                else:raise RuntimeError('Startup timeout')
                for route in ['/online_count','/blog/views?slugs=preflight','/blog/community/comments/about','/music/list']:
                    with urllib.request.urlopen(f'http://127.0.0.1:{port}'+route,timeout=5) as response:
                        assert response.status==200,route
                print('PASS: real Waitress entry, non-project cwd, isolated data, five HTTP routes')
            finally:
                process.terminate()
                try:process.wait(timeout=10)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser();parser.add_argument('--code-root',type=Path,default=ROOT);args=parser.parse_args()
    main(args.code_root.resolve())
