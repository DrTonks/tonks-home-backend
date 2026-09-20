"""Create a code-only ZIP release with SHA-256 inventory. Never includes live data."""
import argparse, hashlib, json, zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
ROOT_FILES=['scripts/preflight.py','server.py','report_app.py','upload_agent_stats.py','manage_article_views.py','requirements.txt','example.jsonc','comment_moderation_prompt.md','.env.example']

def release_files(root=ROOT):
    files=[root/name for name in ROOT_FILES]
    for folder in ['sleepy_app','pet_ai','clients','jsonc_parser']:
        files.extend(p for p in (root/folder).rglob('*') if p.is_file() and not p.is_symlink() and p.suffix in {'.py','.json','.md'} and '__pycache__' not in p.parts)
    return sorted(files)

def package(output):
    files=release_files();inventory={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'x',compression=zipfile.ZIP_DEFLATED) as archive:
        for p in files:archive.write(p,p.relative_to(ROOT).as_posix())
        archive.writestr('release-manifest.json',json.dumps({'schema':1,'files':inventory},indent=2))
    return len(files)
if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    print('Packaged',package(args.output),'files; runtime data and credentials excluded')
