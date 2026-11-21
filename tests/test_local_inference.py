"""Offline local inference configuration and routing tests."""
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import unittest
from unittest import mock

sys.path.append(str(Path(__file__).resolve().parents[1] / "engine/spore_provider_fix/work/scratch"))
import spore_model_router as router


class LocalInferenceTests(unittest.TestCase):
    def local_env(self):
        return mock.patch.dict(os.environ, {
            "SPORE_LOCAL_BASE_URL": "http://127.0.0.1:8080/v1",
            "SPORE_LOCAL_MODEL": "Inkling-2-Mini-UD-Q4_K_M.gguf",
        })

    def test_custom_provider_is_not_openrouter(self):
        with self.local_env():
            config = router.local_opencode_config()
        self.assertEqual(config["model"], "spore_local/inkling")
        self.assertEqual(config["providers"]["spore_local"]["models"]["inkling"]["modelID"],
                         "Inkling-2-Mini-UD-Q4_K_M.gguf")
        self.assertNotIn("openrouter", json.dumps(config).lower())

    def test_rejects_remote_endpoint_and_missing_model(self):
        with mock.patch.dict(os.environ, {
            "SPORE_LOCAL_BASE_URL": "https://example.com/v1",
            "SPORE_LOCAL_MODEL": "whatever",
        }):
            with self.assertRaises(ValueError):
                router.local_settings()
        with mock.patch.dict(os.environ, {
            "SPORE_LOCAL_BASE_URL": "http://127.0.0.1:8080/v1",
            "SPORE_LOCAL_MODEL": "",
        }):
            with self.assertRaises(ValueError):
                router.local_settings()

    def test_probe_fails_for_wrong_model(self):
        with self.local_env():
            with mock.patch.object(router, "urlopen", return_value=io.BytesIO(
                    b'{"data":[{"id":"other-model"}]}')):
                with self.assertRaises(RuntimeError):
                    router.probe_local()
            with mock.patch.object(router, "urlopen", return_value=io.BytesIO(
                    b'{"data":[{"id":"Inkling-2-Mini-UD-Q4_K_M.gguf"}]}')):
                self.assertEqual(router.probe_local()[1], "Inkling-2-Mini-UD-Q4_K_M.gguf")

    def test_pins_local_model_without_price_requests(self):
        with self.local_env(), sqlite3.connect(":memory:") as db:
            with mock.patch.object(router, "top_ten", side_effect=AssertionError("network used")):
                choice = router.choose(db, run_id="r1", va="0x00c00000",
                                       pass_number=1, provider="local", policy="fixed")
                same = router.choose(db, run_id="r1", va="0x00c00000",
                                     pass_number=1, provider="local", policy="fixed")
            self.assertEqual(choice, same)
            self.assertEqual(choice["estimate_usd"], 0.0)
            with self.assertRaises(ValueError):
                router.choose(db, run_id="r2", va="0x00c00000",
                              pass_number=1, provider="local", policy="cheapest")


if __name__ == "__main__":
    unittest.main()
