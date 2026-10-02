import importlib.util
import copy
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
    def evidence(self):
        receipt = {'version': '1.0.40', 'appId': 'com.mcaststudio.MCast',
                   'bundleSha256': 'a' * 64, 'archiveSha256': 'b' * 64,
                   'hostComponent': {'sha256': 'c' * 64},
                   'sourceProvenance': {'sourceManifestSha256': 'd' * 64}}
        evidence = {'schemaVersion': 1, 'version': '1.0.40', 'appId': receipt['appId'],
                    'architecture': 'x86_64', 'bundleSha256': 'a' * 64,
                    'archiveSha256': 'b' * 64, 'hostComponentSha256': 'c' * 64,
                    'sourceManifestSha256': 'd' * 64, 'checkedAtUtc': '2026-10-03T00:00:00Z',
                    'checks': [{'name': name, 'result': 'passed', 'summary': 'Verified.'}
                               for name in sorted(release.REQUIRED_RUNTIME_CHECKS | {'flatpak-shutdown'})]}
        return evidence, receipt

    def test_finalization_rejects_wrong_assets_failures_and_missing_runtime_checks(self):
        evidence, receipt = self.evidence()
        self.assertEqual(release.validated_runtime_evidence(evidence, receipt), evidence)
        for field in ['bundleSha256', 'archiveSha256', 'hostComponentSha256', 'sourceManifestSha256']:
            altered = copy.deepcopy(evidence)
            altered[field] = '0' * 64
            with self.subTest(field=field), self.assertRaises(ValueError):
                release.validated_runtime_evidence(altered, receipt)
        for result in ['failed', 'unverified', 'unsupported']:
            altered = copy.deepcopy(evidence)
            altered['checks'][0]['result'] = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                release.validated_runtime_evidence(altered, receipt)
        altered = copy.deepcopy(evidence)
        altered['checks'][0] = altered['checks'][1]
        with self.assertRaises(ValueError):
            release.validated_runtime_evidence(altered, receipt)

    def test_complete_signing_fingerprint_is_required(self):
        for value in ['12345678', 'release-key', '--help', 'A' * 39, 'A' * 41]:
            with self.subTest(value=value), self.assertRaises(Exception):
                release.fingerprint(value)
        self.assertEqual(release.fingerprint('ab' * 20), 'AB' * 20)

    def test_shutdown_may_be_unverified_but_must_be_explicit(self):
        evidence, receipt = self.evidence()
        shutdown = next(check for check in evidence['checks'] if check['name'] == 'flatpak-shutdown')
        shutdown['result'] = 'unverified'
        shutdown['summary'] = 'Normal shutdown was not observed; no further runtime testing was requested.'
        self.assertEqual(release.validated_runtime_evidence(evidence, receipt), evidence)
        for result in ['unsupported', 'failed']:
            shutdown['result'] = result
            with self.subTest(result=result), self.assertRaises(ValueError):
                release.validated_runtime_evidence(evidence, receipt)
        evidence['checks'].remove(shutdown)
        evidence['checks'].append({'name': 'additional-check', 'result': 'passed', 'summary': 'Verified.'})
        with self.assertRaises(ValueError):
            release.validated_runtime_evidence(evidence, receipt)

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
