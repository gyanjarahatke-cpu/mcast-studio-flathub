import importlib.util
import hashlib
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

scripts = Path(__file__).parents[1] / 'scripts'
sys.path.insert(0, str(scripts))
spec = importlib.util.spec_from_file_location('runtime_archive', scripts / 'prepare_runtime_archive.py')
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)


class RuntimeArchiveTests(unittest.TestCase):
    def test_optional_lttng_omission_is_exact_and_version_reviewed(self):
        config = {'includedFrameworks': [{'name': 'Microsoft.NETCore.App', 'version': '10.0.8'}]}
        excluded = runtime.optional_diagnostic_exclusion(Path('libcoreclrtraceptprovider.so'), config)
        self.assertEqual(excluded['runtimeVersion'], '10.0.8')
        self.assertIn('DOTNET_LTTng=0', excluded['reason'])
        for name in ['libcoreclr.so', 'libhostpolicy.so', 'libmscordaccore.so',
                     'System.Diagnostics.Tracing.dll', 'other/libcoreclrtraceptprovider.so']:
            self.assertIsNone(runtime.optional_diagnostic_exclusion(Path(name), config))
        config['includedFrameworks'][0]['version'] = '11.0.0'
        with self.assertRaisesRegex(ValueError, 'Review the optional tracing'):
            runtime.optional_diagnostic_exclusion(Path('libcoreclrtraceptprovider.so'), config)

    def test_shipped_elf_missing_dependency_still_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            stage = Path(folder).resolve()
            library = stage / 'required.so'
            header = bytearray(20)
            header[:6] = b'\x7fELF\x02\x01'
            header[18:20] = b'\x3e\x00'
            library.write_bytes(header)
            result = runtime.subprocess.CompletedProcess([], 0, 'librequired.so => not found')
            with patch.object(runtime.subprocess, 'check_output', side_effect=['', '(NEEDED) [librequired.so]']), \
                 patch.object(runtime.subprocess, 'run', return_value=result), \
                 self.assertRaisesRegex(ValueError, 'unavailable in GNOME Platform 50'):
                runtime.validate_elf(library, stage)

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
