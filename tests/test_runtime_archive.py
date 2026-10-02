import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

scripts = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('runtime_archive', scripts / 'prepare_runtime_archive.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeArchiveTests(unittest.TestCase):
    def test_source_manifest_rejects_changed_files_and_escaping_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            repository = Path(folder).resolve()
            source = repository / 'source.cpp'
            source.write_bytes(b'current source')
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            manifest = repository / 'source-manifest.json'
            value = {'revision': 'a' * 40, 'expected': {'source.cpp': digest}}
            manifest.write_text(json.dumps(value))
            self.assertEqual(runtime.verify_source_manifest(manifest, repository), value)
            source.write_bytes(b'changed source')
            with self.assertRaises(ValueError):
                runtime.verify_source_manifest(manifest, repository)
            value['expected'] = {'../escape': digest}
            manifest.write_text(json.dumps(value))
            with self.assertRaises(ValueError):
                runtime.verify_source_manifest(manifest, repository)

    def test_only_canonical_release_output_is_accepted(self):
        with tempfile.TemporaryDirectory() as folder:
            for suffix, accepted in [('src/MCastStudio/bin/linux-x64/Release', True),
                                     ('src/MCastStudio/bin/linux-x64/Debug', False),
                                     ('shadow/Release', False),
                                     ('src/MCastStudio/bin/linux-arm64/Release', False)]:
                path = Path(folder) / suffix
                path.mkdir(parents=True)
                (path / 'MCast').write_bytes(b'application')
                if accepted:
                    self.assertEqual(runtime.validate_canonical(path), path.resolve())
                else:
                    with self.assertRaises(ValueError):
                        runtime.validate_canonical(path)

    def test_test_probes_and_symbols_are_not_deployment_files(self):
        for name in ['MCast.Native.AudioTests', 'MCast.Native.AudioTests.deps.json',
                     'MCast.Managed.CrashProbe', 'MCast.NdiBridge.SmokeTests',
                     'MCast.Camera.Consumer', 'MCast.pdb']:
            with self.subTest(name=name):
                self.assertTrue(runtime.development_file(Path(name)))
        for name in ['MCast', 'MCast.dll', 'MCast.Native.Runtime.so',
                     'MCast.VirtualCamera.Setup', 'MCast.Browser.Host']:
            with self.subTest(name=name):
                self.assertFalse(runtime.development_file(Path(name)))


if __name__ == '__main__':
    unittest.main()
