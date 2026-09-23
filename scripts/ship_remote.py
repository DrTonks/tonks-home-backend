"""Run from SSH stdin: python3 - <base64 JSON parameters> < ship_remote.py.

The release ZIP contains code only. Runtime data stays in /var/sleepy. Ordinary
failures during activation restore code and (if requested) the previous .env.
"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import uuid
import zipfile

ROOT = Path('/var/sleepy')
SKIP = {'requirements.txt', 'example.jsonc', '.env.example',
        'report_app.py', 'upload_agent_stats.py', 'scripts/preflight.py'}
MAX_ARCHIVE = 15 * 1024 * 1024


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def code_digest(data):
    return digest(data.replace(b'\r\n', b'\n'))


def validate(parameters):
    require(parameters.get('remoteDir') == str(ROOT), 'unrecognized remote directory')
    release_id = parameters.get('releaseId')
    require(isinstance(release_id, str) and str(uuid.UUID(release_id)) == release_id,
            'invalid release ID')
    sha = parameters.get('archiveSha256')
    require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha), 'invalid archive hash')
    require(parameters.get('action') in {'prepare', 'dry-run', 'activate', 'discard'}, 'invalid action')
    require(type(parameters.get('withEnv')) is bool, 'invalid env option')
    require(ROOT.is_dir() and not ROOT.is_symlink(), 'production root unavailable')
    return release_id


def paths(release_id):
    directory = ROOT / '.releases' / release_id
    return directory, directory / 'release.zip', directory / 'env.candidate'


def run(command, timeout=35):
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f'{command[0]} {command[1]} failed (exit {result.returncode}): {result.stderr[-700:]}')
    return result.stdout


def pm2_processes():
    processes = json.loads(run(['pm2', 'jlist']))
    by_name = {p['name']: p for p in processes}
    for name in ('sleepy-server', 'sleepy-notifications'):
        require(name in by_name, f'PM2 process missing: {name}')
        details = by_name[name]['pm2_env']
        require(details.get('pm_cwd') == str(ROOT), f'unexpected PM2 cwd: {name}')
        expected = str(ROOT / 'venv/bin/python') if name == 'sleepy-server' else 'none'
        require(details.get('exec_interpreter') == expected,
                f'unexpected PM2 interpreter: {name}')
    return by_name


def verify_archive(archive, expected_hash):
    require(archive.is_file() and not archive.is_symlink() and archive.stat().st_size <= MAX_ARCHIVE,
            'release archive missing or too large')
    require(digest(archive.read_bytes()) == expected_hash, 'release archive hash mismatch')
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read('release-manifest.json'))
        files = manifest.get('files')
        require(manifest.get('schema') == 1 and isinstance(files, dict), 'invalid release manifest')
        names = bundle.namelist()
        require(len(names) == len(set(names)) and 'release-manifest.json' not in files and
                set(names) == set(files) | {'release-manifest.json'}, 'archive inventory mismatch')
        require(len(files) <= 300 and 'server.py' in files, 'invalid release file list')
        payload = {}
        total = 0
        for name, sha in files.items():
            parsed = PurePosixPath(name)
            require(name == parsed.as_posix() and not parsed.is_absolute() and
                    all(part not in ('.', '..') for part in parsed.parts) and
                    (not name.startswith('.') or name == '.env.example'), 'unsafe archive path')
            info = bundle.getinfo(name)
            require(not info.is_dir() and (info.external_attr >> 16) & 0o170000 in (0, 0o100000),
                    'archive contains non-regular file')
            require(info.file_size <= 2 * 1024 * 1024, 'file exceeds release size limit')
            total += info.file_size
            require(total <= MAX_ARCHIVE * 3, 'release exceeds extraction budget')
            content = bundle.read(name)
            require(digest(content) == sha, f'release manifest mismatch: {name}')
            payload[name] = content
    return payload


def release_diff(payload):
    changed = []
    for name, content in payload.items():
        target = ROOT / name
        if name in SKIP:
            if name == 'requirements.txt' and target.is_file():
                require(target.read_bytes().replace(b'\r\n', b'\n') == content.replace(b'\r\n', b'\n'),
                        'requirements.txt differs; update cloud venv separately before shipping')
            continue
        require(not target.is_symlink(), f'refusing symlink target: {name}')
        for parent in target.parents:
            if parent == ROOT:
                break
            require(not parent.is_symlink(), f'refusing symlink parent: {name}')
        if not target.is_file() or target.read_bytes().replace(b'\r\n', b'\n') != content.replace(b'\r\n', b'\n'):
            changed.append(name)
    return changed


def deployment_state(payload):
    marker = ROOT / '.releases' / 'ship-state.json'
    require(not marker.is_symlink(), 'refusing symlink release state')
    if not marker.exists():
        return None, []
    state = json.loads(marker.read_text(encoding='utf-8'))
    hashes = state.get('files')
    require(state.get('schema') == 1 and isinstance(hashes, dict), 'invalid release state')
    drift = []
    for name, old_hash in hashes.items():
        require(isinstance(name, str) and isinstance(old_hash, str), 'invalid release state entry')
        parsed = PurePosixPath(name)
        require(name == parsed.as_posix() and not parsed.is_absolute() and
                all(part not in ('.', '..') for part in parsed.parts) and name not in SKIP,
                'unsafe release state entry')
        target = ROOT / name
        if not target.is_file() or target.is_symlink() or code_digest(target.read_bytes()) != old_hash:
            drift.append(name)
    for name in payload:
        if name not in SKIP and name not in hashes and (ROOT / name).exists():
            drift.append(name)
    return state, drift


def save_deployment_state(payload):
    files = {}
    for name in payload:
        if name not in SKIP:
            files[name] = code_digest((ROOT / name).read_bytes())
    marker = ROOT / '.releases' / 'ship-state.json'
    temporary = marker.with_suffix('.json.tmp')
    temporary.write_text(json.dumps({'schema': 1, 'files': files}, sort_keys=True), encoding='utf-8')
    os.replace(temporary, marker)


def preflight(directory, payload):
    stage = directory / 'stage'
    if stage.exists():
        shutil.rmtree(stage)
    for name, content in payload.items():
        destination = stage / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)
    env = dict(os.environ)
    env['TMPDIR'] = '/tmp'
    env['NO_PROXY'] = '127.0.0.1,localhost'
    env['no_proxy'] = '127.0.0.1,localhost'
    result = subprocess.run([str(ROOT / 'venv/bin/python'), str(stage / 'scripts/preflight.py'),
                             '--code-root', str(stage)], cwd=stage, env=env,
                            capture_output=True, text=True, timeout=70)
    require(result.returncode == 0, f'cloud preflight failed: {result.stderr[-1200:]} {result.stdout[-700:]}')


def env_keys(candidate):
    data = candidate.read_bytes()
    require(0 < len(data) <= 1024 * 1024 and b'\x00' not in data, 'invalid env candidate')
    text = data.decode('utf-8')
    keys = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if line.startswith('export '):
            line = line[7:]
        require(re.match(r'^[A-Za-z_][A-Za-z_0-9]*=', line) is not None,
                'env candidate has a malformed assignment')
        keys.append(line.split('=', 1)[0])
    require(keys, 'env candidate contains no keys')
    return set(keys)


def health():
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(30):
        try:
            processes = pm2_processes()
            if all(processes[name]['pm2_env']['status'] == 'online'
                   for name in ('sleepy-server', 'sleepy-notifications')):
                for route in ('/get/status_list', '/blog/community/comments/about'):
                    with opener.open('http://127.0.0.1:9010' + route, timeout=3) as response:
                        require(response.status == 200, f'health check failed: {route}')
                return
        except (OSError, ValueError, RuntimeError, KeyError):
            pass
        time.sleep(1)
    raise RuntimeError('PM2 or HTTP health check failed after 30 seconds')


def replace_file(source, target, mode=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.ship-new')
    shutil.copy2(source, temporary)
    if mode is not None:
        temporary.chmod(mode)
    os.replace(temporary, target)


def restart_processes():
    for name in ('sleepy-server', 'sleepy-notifications'):
        run(['pm2', 'restart', name, '--update-env'])


def activate(directory, payload, changed, with_env):
    backup = ROOT / '.release-backups' / directory.name
    require(not backup.exists(), 'release backup already exists')
    backup.mkdir(parents=True)
    existing = []
    for name in changed:
        target = ROOT / name
        if target.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
            existing.append(name)
    old_env = ROOT / '.env'
    require(not old_env.is_symlink(), 'refusing symlink production .env')
    if with_env and old_env.is_file():
        shutil.copy2(old_env, backup / '.env')
    (backup / 'manifest.json').write_text(json.dumps({'changed': changed, 'existing': existing,
                                                      'withEnv': with_env}), encoding='utf-8')
    stop_attempted = False
    try:
        stop_attempted = True
        for name in ('sleepy-server', 'sleepy-notifications'):
            run(['pm2', 'stop', name])
        for name in changed:
            replace_file(directory / 'stage' / name, ROOT / name)
        if with_env:
            replace_file(directory / 'env.candidate', old_env, 0o600)
        restart_processes()
        health()
        save_deployment_state(payload)
        return {'ok': True, 'action': 'activate', 'releaseId': directory.name,
                'changedCodeFiles': len(changed), 'envUpdated': with_env,
                'backup': str(backup), 'health': 'online'}
    except Exception as error:
        try:
            if stop_attempted:
                for name in changed:
                    target = ROOT / name
                    if name in existing:
                        replace_file(backup / name, target)
                    else:
                        target.unlink(missing_ok=True)
                if with_env:
                    if (backup / '.env').exists():
                        replace_file(backup / '.env', old_env, 0o600)
                    else:
                        old_env.unlink(missing_ok=True)
                restart_processes()
                health()
        except Exception as rollback_error:
            raise RuntimeError(f'activation failed ({error}); automatic rollback also failed ({rollback_error}); inspect {backup}') from rollback_error
        raise RuntimeError(f'activation failed; previous files restored: {error}') from error


def main(parameters):
    release_id = validate(parameters)
    releases = ROOT / '.releases'
    releases.mkdir(mode=0o700, exist_ok=True)
    require(not releases.is_symlink(), 'release directory must not be a symlink')
    directory, archive, candidate = paths(release_id)
    with (releases / '.ship.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        action = parameters['action']
        if action == 'prepare':
            directory.mkdir(mode=0o700, exist_ok=False)
            return {'ok': True, 'archivePath': str(archive), 'envPath': str(candidate)}
        if action == 'discard':
            require(not directory.is_symlink(), 'refusing symlink staging directory')
            if directory.exists():
                shutil.rmtree(directory)
            return {'ok': True, 'action': 'discard'}
        require(directory.is_dir() and not directory.is_symlink(), 'release staging missing')
        payload = verify_archive(archive, parameters['archiveSha256'])
        with_env = parameters['withEnv']
        keys = env_keys(candidate) if with_env else set()
        processes = pm2_processes()
        if with_env:
            for name in ('sleepy-server', 'sleepy-notifications'):
                configured = processes[name]['pm2_env'].get('SLEEPY_ENV_FILE')
                require(not configured or configured == str(ROOT / '.env'),
                        f'{name} reads an .env outside the managed path')
        shadowed = sorted(keys & set().union(*(set(processes[name]['pm2_env'].keys())
                             for name in ('sleepy-server', 'sleepy-notifications'))))
        changed = release_diff(payload)
        state, drift = deployment_state(payload)
        preflight(directory, payload)
        if action == 'dry-run':
            return {'ok': True, 'action': 'dry-run', 'releaseId': release_id,
                    'changedCodeFiles': len(changed), 'changedCodePaths': changed,
                    'cloudDrift': drift, 'bootstrapRequiresReview': state is None and bool(changed),
                    'envWouldUpdate': with_env,
                    'envKeysShadowedByPM2': shadowed, 'cloudPreflight': 'passed'}
        require(not drift, 'cloud code differs from last deployment: ' + ', '.join(drift))
        require(state is not None or not changed,
                'first deployment would replace untracked cloud code; reconcile cloud changes first')
        require(not shadowed, 'PM2 environment overrides keys in candidate .env: ' + ', '.join(shadowed))
        return activate(directory, payload, changed, with_env)


if __name__ == '__main__':
    try:
        params = json.loads(base64.b64decode(sys.argv[1], validate=True))
        print(json.dumps(main(params)))
    except Exception as exc:
        print(json.dumps({'ok': False, 'error': str(exc)}))
        sys.exit(1)
