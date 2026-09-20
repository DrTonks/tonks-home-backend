import ast
import runpy
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]

class ClientEntrypointTests(unittest.TestCase):
    def test_launchers_dispatch_without_changing_arguments(self):
        for name in ['report_app','upload_agent_stats']:
            with self.subTest(name=name), patch('runpy.run_module') as dispatch:
                original=list(sys.argv)
                runpy.run_path(str(ROOT/(name+'.py')),run_name='__main__')
                dispatch.assert_called_once_with('clients.'+name,run_name='__main__')
                self.assertEqual(sys.argv,original)

    def test_upload_help_works_from_unrelated_directory(self):
        result=subprocess.run([sys.executable,str(ROOT/'upload_agent_stats.py'),'--help'],cwd=ROOT.parent,capture_output=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stderr)
        for flag in [b'--server',b'--secret',b'--no-compute',b'--dry-run']:self.assertIn(flag,result.stdout)

    def test_bat_still_calls_compatible_paths(self):
        text=(ROOT/'start_server.bat').read_text(encoding='utf-8')
        for path in ['local.env.bat','upload_agent_stats.py','report_app.py']:self.assertIn(path,text)
        for name in ['SLEEPY_PYTHON','SLEEPY_SERVER_URL','SLEEPY_ADMIN_SECRET','SLEEPY_STATUS_SECRET']:self.assertIn(name,text)
