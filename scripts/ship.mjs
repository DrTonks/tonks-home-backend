import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { spawnSync } from 'node:child_process';
import { createHash, randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';
import SftpClient from 'ssh2-sftp-client';

const root = fileURLToPath(new URL('../', import.meta.url));
const args = process.argv.slice(2);
const usage = '用法: pnpm ship [--dry-run] [--env-file /path/to/production.env]；pnpm ship:check';
let mode = 'deploy';
let envFile;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--check' && mode === 'deploy') mode = 'check';
  else if (args[i] === '--dry-run' && mode === 'deploy') mode = 'dry-run';
  else if (args[i] === '--env-file' && !envFile && i + 1 < args.length) envFile = path.resolve(args[++i]);
  else throw new Error(usage);
}
if (envFile && mode === 'check') throw new Error('--env-file 只能用于发布或预演');

function python() {
  const names = [process.env.SLEEPY_PYTHON, path.join(root, '.venv/bin/python'), path.join(root, 'venv/bin/python'), 'python3.12', 'python3.11', 'python3.10', 'python3'];
  for (const name of names.filter(Boolean)) {
    if (name.includes('/') && !fs.existsSync(name)) continue;
    const result = spawnSync(name, ['-c', 'import sys, flask, waitress; assert sys.version_info >= (3, 10)'], { stdio: 'ignore' });
    if (result.status === 0) return name;
  }
  throw new Error('需要 Python 3.10+ 及 requirements.txt 中的依赖；先创建 .venv 并安装依赖，或设置 SLEEPY_PYTHON');
}

function run(executable, commandArgs) {
  const env = { ...process.env, TMPDIR: fs.realpathSync(os.tmpdir()), NO_PROXY: '127.0.0.1,localhost', no_proxy: '127.0.0.1,localhost' };
  const result = spawnSync(executable, commandArgs, { cwd: root, env, encoding: 'utf8', maxBuffer: 8 * 1024 * 1024, windowsHide: true });
  const output = `${result.stdout || ''}\n${result.stderr || ''}`;
  if (result.status !== 0) {
    process.stderr.write(output.slice(-10000));
    throw new Error(`${commandArgs[0]} 失败，已停止发布`);
  }
  if (process.env.SLEEPY_SHIP_VERBOSE === '1') process.stdout.write(output);
  else for (const line of output.split(/\r?\n/)) {
    if (/^(Ran \d+ tests|OK|PASS:|Packaged \d+ files)/.test(line)) console.log(line);
  }
}

function credentials() {
  let user = process.env.DEPLOY_USER;
  let host = process.env.DEPLOY_HOST;
  let password = process.env.DEPLOY_PASS;
  const credentialFile = process.env.SLEEPY_SSH_FILE || path.resolve(root, '../serverSSH.txt');
  if (!host && !password && !process.env.DEPLOY_KEY_FILE && fs.existsSync(credentialFile)) {
    const [address, storedPassword] = fs.readFileSync(credentialFile, 'utf8').trim().split(/\r?\n/);
    const match = /^([^@\s]+)@([^\s]+)$/.exec(address || '');
    if (!match) throw new Error('SSH 凭据文件第一行应为 user@host');
    user ||= match[1]; host ||= match[2]; password ||= storedPassword;
  }
  const privateKey = process.env.DEPLOY_KEY_FILE ? fs.readFileSync(process.env.DEPLOY_KEY_FILE) : undefined;
  if (!host || !user || (!privateKey && !password)) throw new Error('请设置 DEPLOY_HOST/DEPLOY_USER 和 DEPLOY_PASS 或 DEPLOY_KEY_FILE');
  return { host, username: user, port: Number(process.env.DEPLOY_PORT || 22), ...(privateKey ? { privateKey } : { password }) };
}

function checkGitBase() {
  const releaseSources = [
    'server.py', 'manage_article_views.py', 'comment_moderation_prompt.md',
    'requirements.txt', 'sleepy_app', 'pet_ai', 'clients', 'jsonc_parser',
    'scripts/preflight.py', 'scripts/package_release.py', 'scripts/ship.mjs',
    'scripts/ship_remote.py', 'package.json', 'pnpm-lock.yaml',
    'pnpm-workspace.yaml', 'tests',
  ];
  const dirty = spawnSync('git', ['status', '--porcelain', '--untracked-files=normal', '--', ...releaseSources],
    { cwd: root, encoding: 'utf8', windowsHide: true });
  if (dirty.status !== 0 || dirty.stdout.trim()) {
    throw new Error('发布代码、测试或部署脚本有未提交改动；请先提交或清理');
  }
  const fetched = spawnSync('git', ['fetch', 'origin', 'main'], { cwd: root, encoding: 'utf8', windowsHide: true });
  if (fetched.status !== 0) throw new Error(`无法核对远程 Git 分支：${(fetched.stderr || '').slice(-1000)}`);
  const relation = spawnSync('git', ['merge-base', '--is-ancestor', 'FETCH_HEAD', 'HEAD'], { cwd: root, stdio: 'ignore' });
  if (relation.status !== 0) throw new Error('本地 HEAD 未包含 GitHub origin/main 的最新提交；先合并或变基后发布');
}

function remote(sftp, parameters, timeoutMs = 180000) {
  const source = fs.readFileSync(new URL('./ship_remote.py', import.meta.url));
  const encoded = Buffer.from(JSON.stringify(parameters)).toString('base64');
  return new Promise((resolve, reject) => {
    let channel, done = false;
    const finish = (error, result) => { if (done) return; done = true; clearTimeout(timer); error ? reject(error) : resolve(result); };
    const timer = setTimeout(() => { channel?.signal('TERM'); finish(new Error('远端发布超时，请检查 PM2 状态和 .release-backups 后再重试')); }, timeoutMs);
    sftp.client.exec(`exec python3 - '${encoded}'`, (error, stream) => {
      if (error) return finish(error);
      channel = stream;
      let output = '', diagnostics = '';
      stream.on('data', part => { if (output.length < 100000) output += part.toString(); });
      stream.stderr.on('data', part => { if (diagnostics.length < 30000) diagnostics += part.toString(); });
      stream.on('error', error => finish(error));
      stream.on('close', code => {
        let result;
        try { result = JSON.parse(output.trim()); }
        catch { return finish(new Error(`远端脚本无有效结果 (${code}): ${diagnostics}`)); }
        if (code !== 0 || !result.ok) return finish(new Error(result.error || diagnostics || `远端失败 (${code})`));
        finish(null, result);
      });
      stream.end(source);
    });
  });
}

const py = python();
if (mode !== 'check') checkGitBase();
console.log('检查本地测试与独立预检…');
run(py, ['-m', 'unittest', 'discover', '-s', 'tests']);
run(py, ['scripts/preflight.py']);
if (mode === 'check') { console.log('本地检查通过'); process.exit(0); }

const temp = fs.mkdtempSync(path.join(os.tmpdir(), 'sleepy-ship-'));
const releaseId = randomUUID();
const archive = path.join(temp, 'release.zip');
const remoteDir = process.env.SLEEPY_REMOTE_DIR || '/var/sleepy';
if (remoteDir !== '/var/sleepy') throw new Error('当前发布流程只支持已核实的 /var/sleepy');
try {
  run(py, ['scripts/package_release.py', '--output', archive]);
  const hash = createHash('sha256').update(fs.readFileSync(archive)).digest('hex');
  if (envFile) {
    const stat = fs.lstatSync(envFile);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.size === 0 || stat.size > 1024 * 1024) throw new Error('env 文件必须是非空、非符号链接且不超过 1 MiB 的普通文件');
  }
  const sftp = new SftpClient();
  let prepared = false, activationAttempted = false;
  try {
    await sftp.connect(credentials());
    const common = { releaseId, remoteDir, archiveSha256: hash, withEnv: Boolean(envFile) };
    const staging = await remote(sftp, { ...common, action: 'prepare' });
    prepared = true;
    await sftp.fastPut(archive, staging.archivePath);
    if (envFile) await sftp.fastPut(envFile, staging.envPath);
    activationAttempted = mode === 'deploy';
    const result = await remote(sftp, { ...common, action: mode === 'dry-run' ? 'dry-run' : 'activate' }, 240000);
    console.log(JSON.stringify(result, null, 2));
  } finally {
    if (prepared && !activationAttempted) {
      await remote(sftp, { action: 'discard', releaseId, remoteDir, archiveSha256: hash, withEnv: Boolean(envFile) }).catch(error => console.warn(`临时发布包清理失败: ${error.message}`));
    }
    await sftp.end().catch(() => {});
  }
} finally { fs.rmSync(temp, { recursive: true, force: true }); }
