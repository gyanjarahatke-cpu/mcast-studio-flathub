import hashlib
import importlib.util
import io
import json
import tarfile
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

spec = importlib.util.spec_from_file_location('prepare', Path(__file__).parents[1] / 'scripts/prepare_release.py')
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)


class ReleasePreparationTests(unittest.TestCase):
    def archive(self, root, extra=None, omit=None):
        path = root / 'release.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name in sorted(prepare.REQUIRED | {'Resources/icon.png'}):
                if name == omit:
                    continue
                data = bytearray(64)
                if name == 'MCast':
                    data[:6] = b'\x7fELF\x02\x01'
                    data[18:20] = b'\x3e\x00'
                item = tarfile.TarInfo('./' + name)
                item.size = len(data)
                item.mode = 0o755 if name == 'MCast' else 0o644
                archive.addfile(item, io.BytesIO(data))
            if extra is not None:
                archive.addfile(extra)
        return path, hashlib.sha256(path.read_bytes()).hexdigest()

    def test_candidate_and_stable_manifests(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive, digest = self.archive(root)
            for version, expected_branch in [('1.0.0-beta.1', 'beta'), ('1.0.0', 'stable')]:
                out = root / version
                branch = prepare.generate(archive, 'https://example.org/mcast.tar.gz', digest,
                    version, '2026-09-07', 'https://example.org/screenshot.png', out)
                self.assertEqual(branch, expected_branch)
                manifest = json.loads((out / 'com.mcaststudio.MCast.json').read_text())
                self.assertEqual(manifest['modules'][0]['sources'][0]['sha256'], digest)
                self.assertEqual(manifest['default-branch'], branch)
                for source in manifest['modules'][0]['sources'][1:]:
                    self.assertTrue((out / source['path']).is_file())
                self.assertTrue((out / 'mcast-studio').read_bytes().startswith(b'#!/bin/sh\n'))
                self.assertNotIn(b'\r', (out / 'mcast-studio').read_bytes())
                release = ET.parse(out / 'com.mcaststudio.MCast.metainfo.xml').find('releases/release')
                self.assertEqual(release.attrib['version'], version)
                self.assertFalse(any(p.suffix == '.gz' for p in out.iterdir()))

    def test_missing_runtime_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, digest = self.archive(Path(folder), omit='libcoreclr.so')
            with self.assertRaisesRegex(ValueError, 'self-contained'):
                prepare.verify_archive(archive, digest)

    def test_local_candidate_uses_same_package_without_fabricated_urls(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive, digest = self.archive(root)
            out = root / 'local'
            prepare.generate(archive, None, digest, '1.0.0-beta.1', '2026-09-07', None, out, local_archive=True)
            manifest = json.loads((out / 'com.mcaststudio.MCast.json').read_text())
            source = manifest['modules'][0]['sources'][0]
            self.assertEqual(source['path'], str(archive.resolve()))
            self.assertEqual(source['sha256'], digest)
            self.assertNotIn('url', source)
            self.assertEqual(manifest['command'], 'mcast-studio')
            self.assertIsNone(ET.parse(out / 'com.mcaststudio.MCast.metainfo.xml').find('screenshots'))

    def test_public_candidate_requires_screenshot(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive, digest = self.archive(root)
            with self.assertRaisesRegex(ValueError, 'screenshot'):
                prepare.generate(archive, 'https://example.org/mcast.tar.gz', digest,
                                 '1.0.0-beta.1', '2026-09-07', None, root / 'out')

    def test_local_candidate_rejects_public_url(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive, digest = self.archive(root)
            with self.assertRaisesRegex(ValueError, 'must not specify'):
                prepare.generate(archive, 'https://example.org/mcast.tar.gz', digest,
                                 '1.0.0-beta.1', '2026-09-07', None, root / 'out', local_archive=True)

    def test_wrong_hash_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            archive, _ = self.archive(Path(folder))
            with self.assertRaisesRegex(ValueError, 'does not match'):
                prepare.verify_archive(archive, '0' * 64)

    def test_unsafe_archive_entries_rejected(self):
        for filename in ['../escape', '/absolute', '.git/config', 'source.cpp', 'private.pfx',
                         'MCast.NdiBridge.SmokeTests']:
            with self.subTest(filename=filename), tempfile.TemporaryDirectory() as folder:
                archive, digest = self.archive(Path(folder), tarfile.TarInfo(filename))
                with self.assertRaises(ValueError):
                    prepare.verify_archive(archive, digest)

    def test_escaping_link_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            link = tarfile.TarInfo('escape')
            link.type = tarfile.SYMTYPE
            link.linkname = '../outside'
            archive, digest = self.archive(Path(folder), link)
            with self.assertRaisesRegex(ValueError, 'escaping link'):
                prepare.verify_archive(archive, digest)

    def test_private_or_insecure_url_rejected(self):
        for url in ['http://example.org/a', 'https://name:secret@example.org/a',
                    'https://example.org/a?token=secret', 'file:///tmp/release']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                prepare.https_url(url)

    def test_missing_link_target_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            link = tarfile.TarInfo('compiler.so')
            link.type = tarfile.SYMTYPE
            link.linkname = 'compiler.so.1'
            archive, digest = self.archive(Path(folder), link)
            with self.assertRaisesRegex(ValueError, 'missing link target'):
                prepare.verify_archive(archive, digest)

    def test_cyclic_link_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            link = tarfile.TarInfo('compiler.so')
            link.type = tarfile.SYMTYPE
            link.linkname = 'compiler.so'
            archive, digest = self.archive(Path(folder), link)
            with self.assertRaisesRegex(ValueError, 'cyclic link'):
                prepare.verify_archive(archive, digest)


if __name__ == '__main__':
    unittest.main()
