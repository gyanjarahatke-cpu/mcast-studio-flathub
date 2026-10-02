import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

scripts = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('signed_release', scripts / 'build_signed_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class SignedReleaseSafetyTests(unittest.TestCase):
    def test_complete_signing_fingerprint_is_required(self):
        for value in ['12345678', 'release-key', '--help', 'A' * 39, 'A' * 41]:
            with self.subTest(value=value), self.assertRaises(Exception):
                release.fingerprint(value)
        self.assertEqual(release.fingerprint('ab' * 20), 'AB' * 20)

    def test_existing_release_files_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'release'
            output.mkdir()
            asset = output / 'published.flatpak'
            asset.write_bytes(b'existing release')
            with self.assertRaises(ValueError):
                release.empty_directory(output)
            self.assertEqual(asset.read_bytes(), b'existing release')

    def test_new_or_empty_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'release'
            self.assertEqual(release.empty_directory(output), output.resolve())
            output.mkdir()
            self.assertEqual(release.empty_directory(output), output.resolve())


if __name__ == '__main__':
    unittest.main()
