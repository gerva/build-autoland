import unittest
from autoland.config import config
from StringIO import StringIO
from tempfile import mkstemp
import os

cfg = """
[defaults]
cfg1 = http://www
cfg2 = 1
[extra]
cfg1 = http://xxx
cfg2 = 2
"""
cfg_dict = dict(
    cfg1="http://www", cfg2="1",
    extra_cfg1="http://xxx", extra_cfg2="2",
)


class TestAutolandConfig(unittest.TestCase):

    def setUp(self):
        _, self.tmp = mkstemp()

    def tearDown(self):
        os.remove(self.tmp)

    def test_read_from_fp(self):
        config.read(StringIO(cfg))
        self.assertDictEqual(config, cfg_dict)

    def test_read_from_file(self):
        with open(self.tmp, "w") as f:
            f.write(cfg)
        config.read(self.tmp)
        self.assertDictEqual(config, cfg_dict)
