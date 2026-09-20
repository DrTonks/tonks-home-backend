"""Compare a saved baseline and current checkout using ONLY temporary data.
Usage: python scripts/benchmark_refactor.py --baseline PATH
No external APIs or real uploads are exercised.
"""
import argparse, json, os, subprocess, sys, tempfile, time, statistics, contextlib, io, tracemalloc
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ROUTES=['/','/online_count','/get/status_list','/blog/views?slugs=test','/blog/community/comments/about','/music/list','/todos','/calendar/events']

def probe(code):
    with tempfile.TemporaryDirectory(prefix='sleepy-benchmark-') as directory:
        root=Path(directory);(root/'data.json').write_text(json.dumps({'host':'127.0.0.1','port':9010,'status':0,'status_list':[{'id':0,'name':'Online'}],'todos':[],'music_files':[],'calendar_events':[]}))
        for key in list(os.environ):
            if key.startswith('SLEEPY_'):del os.environ[key]
        os.environ.update(SLEEPY_DATA_DIR=directory,SLEEPY_DATA_FILE=str(root/'data.json'),SLEEPY_ENV_FILE=str(root/'absent.env'),SLEEPY_MUSIC_DIR=str(root/'music'),SLEEPY_GITHUB_CACHE_FILE=str(root/'github.json'))
        for key in ['ANALYTICS','AGENT_ACTIVITY','RECOMMENDATIONS','COMMUNITY']:os.environ['SLEEPY_'+key+'_DB']=str(root/(key+'.sqlite3'))
        sys.path.insert(0,str(ROOT));sys.path.insert(0,str(code));os.chdir(root)
        with contextlib.redirect_stdout(io.StringIO()):
            tracemalloc.start();started=time.perf_counter()
            import server
            startup_ms=(time.perf_counter()-started)*1000
            allocated,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
            client=server.app.test_client();result={}
            for route in ROUTES:
                for _ in range(10):client.get(route)
                samples=[]
                for _ in range(200):
                    start=time.perf_counter_ns();response=client.get(route);samples.append((time.perf_counter_ns()-start)/1e6)
                    if response.status_code!=200:raise RuntimeError((route,response.status_code))
                result[route]={'median_ms':statistics.median(samples),'p95_ms':sorted(samples)[189],'status':response.status_code,'body':response.get_json()}
        result['_process']={'startup_ms':startup_ms,'python_allocated_bytes':allocated,'python_peak_bytes':peak}
        print(json.dumps(result))
        os.chdir(ROOT)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--baseline',type=Path);parser.add_argument('--probe',type=Path);args=parser.parse_args()
    if args.probe:probe(args.probe.resolve())
    else:
        if not args.baseline:parser.error('--baseline is required')
        output={}
        reference=None
        for label,code in [('baseline',args.baseline),('current',ROOT)]:
            runs=[]
            for _ in range(3):
                run=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--probe',str(code.resolve())],capture_output=True,text=True,encoding='utf-8',timeout=90,env={**os.environ,'PYTHONIOENCODING':'utf-8'})
                if run.returncode:raise RuntimeError(run.stderr)
                runs.append(json.loads(run.stdout))
            if reference is None:reference={route:runs[-1][route]['body'] for route in ROUTES}
            else:
                for route in ROUTES:
                    if runs[-1][route]['body']!=reference[route]:raise RuntimeError('Response changed: '+route)
            output[label]={route:{'median_ms':statistics.median(r[route]['median_ms'] for r in runs),'p95_ms':statistics.median(r[route]['p95_ms'] for r in runs)} for route in ROUTES}
            output[label]['_process']={key:statistics.median(r['_process'][key] for r in runs) for key in runs[0]['_process']}
        print(json.dumps(output,indent=2))
