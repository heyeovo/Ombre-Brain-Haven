import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import yaml  # noqa: F401
except ModuleNotFoundError:  # load_config does not parse YAML in these tests.
    sys.modules["yaml"] = SimpleNamespace(safe_load=lambda stream: {})

from utils import load_config


class ConfigPersistenceTest(unittest.TestCase):
    def test_state_env_is_loaded_after_container_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            state_dir.mkdir()
            (state_dir / ".env").write_text(
                'OMBRE_API_KEY="persisted-secret"\n'
                'OMBRE_DEHYDRATION_MODEL=persisted-model\n',
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"OMBRE_STATE_DIR": str(state_dir)}, clear=True):
                config = load_config(str(Path(temp_dir) / "missing-config.yaml"))

            self.assertEqual(config["dehydration"]["api_key"], "persisted-secret")
            self.assertEqual(config["dehydration"]["model"], "persisted-model")

    def test_real_environment_still_overrides_state_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state_dir = Path(temp_dir) / "state"
            state_dir.mkdir()
            (state_dir / ".env").write_text('OMBRE_API_KEY=state-secret\n', encoding="utf-8")
            with patch.dict(os.environ, {
                "OMBRE_STATE_DIR": str(state_dir),
                "OMBRE_API_KEY": "coolify-secret",
            }, clear=True):
                config = load_config(str(Path(temp_dir) / "missing-config.yaml"))

            self.assertEqual(config["dehydration"]["api_key"], "coolify-secret")


if __name__ == "__main__":
    unittest.main()
