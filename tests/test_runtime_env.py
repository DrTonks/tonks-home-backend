import os
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch

from runtime_env import configured_value, load_env_file, migrate_sensitive_data_keys


class FakeDataStore:
    def __init__(self, data):
        self.data = dict(data)
        self.saved = 0

    def dget(self, key):
        return self.data.get(key)

    def save(self):
        self.saved += 1


class RuntimeEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(__file__).resolve().parents[1] / ".test-tmp" / "runtime-env"
        shutil.rmtree(self.temp_dir, ignore_errors=True)
        self.temp_dir.mkdir(parents=True)

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_loads_quoted_values_without_overwriting_process_environment(self):
        path = self.temp_dir / ".env"
        path.write_text(
            'SLEEPY_ADMIN_SECRET="file-secret"\nSLEEPY_STATUS_SECRET=from-file # note\n',
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"SLEEPY_ADMIN_SECRET": "process-secret"}, clear=False):
            os.environ.pop("SLEEPY_STATUS_SECRET", None)
            load_env_file(path)
            self.assertEqual(os.environ["SLEEPY_ADMIN_SECRET"], "process-secret")
            self.assertEqual(os.environ["SLEEPY_STATUS_SECRET"], "from-file")
            os.environ.pop("SLEEPY_STATUS_SECRET", None)

    def test_migrates_only_secrets_with_nonempty_environment_replacements(self):
        store = FakeDataStore(
            {
                "secret": "legacy-status",
                "admin_secret": "legacy-admin",
                "github_token": "legacy-token",
                "todos": [{"id": "keep"}],
            }
        )
        with patch.dict(
            os.environ,
            {
                "SLEEPY_STATUS_SECRET": "new-status",
                "SLEEPY_ADMIN_SECRET": "new-admin",
                "SLEEPY_GITHUB_TOKEN": "",
            },
            clear=False,
        ):
            migrated = migrate_sensitive_data_keys(store)
        self.assertEqual(migrated, ["secret", "admin_secret"])
        self.assertNotIn("secret", store.data)
        self.assertNotIn("admin_secret", store.data)
        self.assertEqual(store.data["github_token"], "legacy-token")
        self.assertEqual(store.data["todos"], [{"id": "keep"}])
        self.assertEqual(store.saved, 1)

    def test_environment_value_has_priority_with_legacy_fallback(self):
        store = FakeDataStore({"admin_secret": "legacy"})
        with patch.dict(os.environ, {"SLEEPY_ADMIN_SECRET": "current"}, clear=False):
            self.assertEqual(
                configured_value(store, "SLEEPY_ADMIN_SECRET", "admin_secret"), "current"
            )
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                configured_value(store, "SLEEPY_ADMIN_SECRET", "admin_secret"), "legacy"
            )


if __name__ == "__main__":
    unittest.main()
