"""Updates keep the version they replace, and can go back to it."""

import os
import tempfile
import unittest
from unittest import mock

from multistream import updater


class UpdaterSafetyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.patches = [mock.patch.object(updater, "UPDATE_DIR", os.path.join(root, "_update")),
                        mock.patch.object(updater, "PREVIOUS_DIR", os.path.join(root, "_previous")),
                        mock.patch.object(updater, "TEST_CHANNEL_FILE", os.path.join(root, "test-channel.txt"))]
        for p in self.patches:
            p.start()
        self.root = root

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def keep(self, version):
        os.makedirs(os.path.join(updater.PREVIOUS_DIR, "_internal"))
        with open(os.path.join(updater.PREVIOUS_DIR, updater.PREVIOUS_MARK), "w") as f:
            f.write(version)

    def test_no_previous_version_at_first(self):
        u = updater.Updater()
        self.assertIsNone(u.info()["previous"])
        self.assertEqual(u.info()["channel"], "stable")

    def test_previous_version_offered(self):
        self.keep("v1.0.80")
        u = updater.Updater()
        u.current = "v1.0.91"
        self.assertEqual(u.info()["previous"], "v1.0.80")
        u.current = "v1.0.80"   # (already gone back to it)
        self.assertIsNone(u.info()["previous"])

    def test_update_script_keeps_the_running_version(self):
        new = os.path.join(self.root, "new")
        os.makedirs(os.path.join(new, "_internal"))
        open(os.path.join(new, "SCCMultistream.exe"), "w").close()
        u = updater.Updater()
        u.current = "v1.0.90"
        u.work_dir = self.root
        text = open(u._write_script(new)).read()
        keep = text.index("rmdir")
        self.assertLess(keep, text.index('robocopy "' + new))   # kept before the new one goes in
        self.assertIn("SCCMultistream.exe", text[keep:text.index("robocopy \"" + new)])
        self.assertIn("echo v1.0.90>", text)

    def test_go_back_script(self):
        self.keep("v1.0.80")
        u = updater.Updater()
        u.work_dir = self.root
        text = open(u._write_rollback_script()).read()
        self.assertIn(updater.PREVIOUS_DIR + "\\_internal", text)
        self.assertIn("SCCMultistream.exe", text)

    def test_busy_blocks_install_and_go_back(self):
        self.keep("v1.0.80")
        u = updater.Updater()
        u.current, u.latest, u.asset, u.can_install = "v1.0.90", "v1.0.91", {"name": "x"}, True
        u.busy = lambda: "A match is being read off the screen"
        with self.assertRaisesRegex(ValueError, "match"):
            u.install_in_background()
        with self.assertRaisesRegex(ValueError, "match"):
            u.rollback_in_background()
        self.assertEqual(u.info()["busy"], "A match is being read off the screen")

    def test_broken_busy_check_does_not_block(self):
        u = updater.Updater()
        u.busy = lambda: 1 / 0
        self.assertIsNone(u.info()["busy"])

    def test_test_channel_reads_the_newest_build(self):
        open(updater.TEST_CHANNEL_FILE, "w").close()
        u = updater.Updater()
        self.assertEqual(u.info()["channel"], "test")
        seen = {}

        class Reply:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'[{"tag_name": "v1.0.95", "body": "test", "assets": []}]'

        def urlopen(request, timeout=0):
            seen["url"] = request.full_url
            return Reply()

        with mock.patch.object(updater.urllib.request, "urlopen", urlopen):
            u.check()
        self.assertTrue(seen["url"].endswith("/releases?per_page=1"))
        self.assertEqual(u.latest, "v1.0.95")


if __name__ == "__main__":
    unittest.main()
