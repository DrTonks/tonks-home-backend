import importlib.util
import tempfile
import unittest
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('release_package',ROOT/'scripts/package_release.py');package=importlib.util.module_from_spec(spec);spec.loader.exec_module(package)
class ReleasePackageTests(unittest.TestCase):
    def test_only_code_and_required_resources_are_packaged(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'release.zip';package.package(output)
            with zipfile.ZipFile(output) as archive:
                names=set(archive.namelist())
                for name in ['sleepy_app/notifications/worker.py','sleepy_app/notifications/delivery.py','sleepy_app/notifications/templates.py','scripts/preflight.py','server.py','sleepy_app/app.py','clients/report_app.py','pet_ai/questions.json','comment_moderation_prompt.md','release-manifest.json']:self.assertIn(name,names)
                for name in names:
                    self.assertFalse(name.endswith(('.sqlite3','.log','.pyc')))
                    self.assertNotIn(name,{'.env','data.json','local.env.bat','article-comments-manifest.json'})
                self.assertFalse(any(n.startswith(('music/','images/')) for n in names))
