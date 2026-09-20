import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from sleepy_app.app import create_app
from sleepy_app.config import Paths, PROJECT_ROOT

class AppFactoryTests(unittest.TestCase):
    def test_two_apps_have_isolated_state_and_data(self):
        with tempfile.TemporaryDirectory() as directory:
            apps=[]
            for name in ['one','two']:
                root=Path(directory)/name;root.mkdir();(root/'data.json').write_text('{}')
                with patch.dict(os.environ, {'SLEEPY_DATA_DIR':str(root), 'SLEEPY_ENV_FILE':str(root/'absent.env')}, clear=True):
                    apps.append(create_app())
            a,b=[app.extensions['sleepy_runtime'] for app in apps]
            a.status_service.update_online_users('test')
            self.assertEqual(b.status_service.get_online_count(),(0,0))
            self.assertIsNot(a.write_lock,b.write_lock)
            self.assertIsNot(a.weather_cache,b.weather_cache)
            self.assertIsNot(a.community_comment_limiter,b.community_comment_limiter)
            self.assertNotEqual(a.community_store.database_path,b.community_store.database_path)
            a.d.dset('isolation',True);b.d.load()
            self.assertNotIn('isolation',b.d.data)

    def test_default_paths_do_not_depend_on_working_directory(self):
        old=os.getcwd()
        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
                os.chdir(directory);paths=Paths.from_env()
                self.assertEqual(paths.data_file,PROJECT_ROOT/'data.json')
                self.assertEqual(paths.music,PROJECT_ROOT/'music')
                self.assertEqual(paths.article_manifest,PROJECT_ROOT/'article-comments-manifest.json')
                os.chdir(old)
        finally:os.chdir(old)

    def test_explicit_relative_paths_are_rooted_in_data_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'SLEEPY_DATA_DIR':directory, 'SLEEPY_COMMUNITY_DB':'custom.sqlite3'}, clear=True):
            self.assertEqual(Paths.from_env().community_db,Path(directory).resolve()/'custom.sqlite3')

    def test_first_start_creates_missing_data_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'new' / 'data'
            data_file = Path(directory) / 'separate' / 'nested' / 'state.json'
            for explicit_file in [False, True]:
                with self.subTest(explicit_file=explicit_file):
                    settings = {'SLEEPY_DATA_DIR': str(root),
                                'SLEEPY_ENV_FILE': str(Path(directory) / 'absent.env')}
                    if explicit_file:
                        settings['SLEEPY_DATA_FILE'] = str(data_file)
                    with patch.dict(os.environ, settings, clear=True):
                        app = create_app()
                    runtime = app.extensions['sleepy_runtime']
                    expected = data_file if explicit_file else root / 'data.json'
                    self.assertEqual(Path(runtime.d.filename), expected)
                    self.assertTrue(expected.is_file())
                    runtime.d.dset('startup_test', True)
                    self.assertTrue(json.loads(expected.read_text(encoding='utf-8'))['startup_test'])
