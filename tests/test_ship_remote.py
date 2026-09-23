"""The deployment helper must never select live data or trust a corrupt ZIP."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


spec = importlib.util.spec_from_file_location(
    'ship_remote', Path(__file__).resolve().parents[1] / 'scripts/ship_remote.py')
ship = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ship)


class ShipRemoteTests(unittest.TestCase):
    def test_archive_inventory_and_hash_must_match(self):
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / 'release.zip'
            data = b'print("hello")\n'
            manifest = {'schema': 1, 'files': {'server.py': hashlib.sha256(data).hexdigest()}}
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('server.py', data)
                bundle.writestr('release-manifest.json', json.dumps(manifest))
            expected = hashlib.sha256(archive.read_bytes()).hexdigest()
            self.assertEqual(ship.verify_archive(archive, expected)['server.py'], data)
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                ship.verify_archive(archive, '0' * 64)
            manifest['files']['server.py'] = '0' * 64
            with zipfile.ZipFile(archive, 'w') as bundle:
                bundle.writestr('server.py', data)
                bundle.writestr('release-manifest.json', json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, 'manifest mismatch'):
                ship.verify_archive(archive, hashlib.sha256(archive.read_bytes()).hexdigest())

    def test_diff_preserves_runtime_and_cloud_only_entrypoints(self):
        with tempfile.TemporaryDirectory() as directory:
            original_root = ship.ROOT
            try:
                ship.ROOT = Path(directory)
                (ship.ROOT / 'server.py').write_bytes(b'old\r\n')
                (ship.ROOT / 'requirements.txt').write_bytes(b'flask\r\n')
                (ship.ROOT / 'report_app.py').write_bytes(b'cloud agent')
                (ship.ROOT / 'data.json').write_bytes(b'private state')
                changed = ship.release_diff({'server.py': b'new\n',
                                             'requirements.txt': b'flask\n',
                                             'report_app.py': b'local wrapper',
                                             'sleepy_app/new.py': b'new module'})
                self.assertEqual(changed, ['server.py', 'sleepy_app/new.py'])
                self.assertEqual((ship.ROOT / 'data.json').read_bytes(), b'private state')
                with self.assertRaisesRegex(ValueError, 'requirements.txt differs'):
                    ship.release_diff({'requirements.txt': b'other dependency\n'})
            finally:
                ship.ROOT = original_root

    def test_env_file_requires_assignments_and_never_prints_values(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / 'env.candidate'
            candidate.write_text('# production\nexport SECRET=hidden\nPORT=9010\n')
            self.assertEqual(ship.env_keys(candidate), {'SECRET', 'PORT'})
            candidate.write_text('not an assignment\n')
            with self.assertRaisesRegex(ValueError, 'malformed'):
                ship.env_keys(candidate)

    def test_failed_restart_restores_code_and_env(self):
        with tempfile.TemporaryDirectory() as directory:
            original_root = ship.ROOT
            try:
                ship.ROOT = Path(directory)
                (ship.ROOT / 'server.py').write_bytes(b'old server')
                (ship.ROOT / '.env').write_bytes(b'SECRET=old\n')
                release = ship.ROOT / '.releases' / 'test-release'
                (release / 'stage').mkdir(parents=True)
                (release / 'stage/server.py').write_bytes(b'new server')
                (release / 'env.candidate').write_bytes(b'SECRET=new\n')
                restarts = []

                def fake_run(command, timeout=35):
                    if command[1] == 'restart':
                        restarts.append(command[2])
                        if len(restarts) == 1:
                            raise RuntimeError('simulated restart failure')
                    return ''

                with patch.object(ship, 'run', side_effect=fake_run), patch.object(ship, 'health'):
                    with self.assertRaisesRegex(RuntimeError, 'previous files restored'):
                        ship.activate(release, {'server.py': b'new server'}, ['server.py'], True)
                self.assertEqual((ship.ROOT / 'server.py').read_bytes(), b'old server')
                self.assertEqual((ship.ROOT / '.env').read_bytes(), b'SECRET=old\n')
                self.assertEqual(restarts, ['sleepy-server', 'sleepy-server', 'sleepy-notifications'])
            finally:
                ship.ROOT = original_root

    def test_cloud_edit_after_release_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            original_root = ship.ROOT
            try:
                ship.ROOT = Path(directory)
                (ship.ROOT / '.releases').mkdir()
                (ship.ROOT / 'server.py').write_bytes(b'published\n')
                payload = {'server.py': b'published\n'}
                self.assertEqual(ship.deployment_state(payload), (None, []))
                ship.save_deployment_state(payload)
                self.assertEqual(ship.deployment_state(payload)[1], [])
                (ship.ROOT / 'server.py').write_bytes(b'cloud hotfix\n')
                self.assertEqual(ship.deployment_state(payload)[1], ['server.py'])
                (ship.ROOT / 'new.py').write_bytes(b'cloud-only implementation\n')
                self.assertEqual(ship.deployment_state({**payload, 'new.py': b'local implementation\n'})[1],
                                 ['server.py', 'new.py'])
            finally:
                ship.ROOT = original_root


if __name__ == '__main__':
    unittest.main()
