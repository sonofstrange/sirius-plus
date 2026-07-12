import tempfile
import unittest
from pathlib import Path

import config


class InstanceSealTests(unittest.TestCase):
    def setUp(self):
        self.key = config.TOKEN_ENCRYPTION_KEY
        self.path = config.INSTANCE_SEAL_FILE
        self.tmp = tempfile.TemporaryDirectory()
        config.TOKEN_ENCRYPTION_KEY = "test-instance-key"
        config.INSTANCE_SEAL_FILE = Path(self.tmp.name) / "instance_seal.json"

    def tearDown(self):
        config.TOKEN_ENCRYPTION_KEY = self.key
        config.INSTANCE_SEAL_FILE = self.path
        self.tmp.cleanup()

    def test_valid_seal_activates_instance(self):
        self.assertFalse(config.instance_seal_is_valid())
        config.create_instance_seal()
        self.assertTrue(config.instance_seal_is_valid())

    def test_tampered_seal_is_rejected(self):
        config.create_instance_seal()
        config.INSTANCE_SEAL_FILE.write_text('{"host":"attacker.example","version":1,"signature":"fake"}')
        self.assertFalse(config.instance_seal_is_valid())


if __name__ == "__main__":
    unittest.main()
