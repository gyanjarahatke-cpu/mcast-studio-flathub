#!/usr/bin/env python3
"""Build signed Linux release assets from the verified canonical runtime archive.

The maintainer supplies an existing persistent signing key. Private keys are never
generated, copied into the output, or uploaded by this tool.
"""
import argparse
import datetime
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import prepare_release
from validate_host_component import validate_host_component


def fingerprint(value):
    value = value.replace(' ', '').upper()
    if not re.fullmatch(r'(?:[0-9A-F]{40}|[0-9A-F]{64})', value):
        raise argparse.ArgumentTypeError('Use the complete release signing-key fingerprint.')
    return value


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def empty_directory(path):
    path = path.resolve()
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError('The work and output directories must be empty; existing releases are never overwritten.')
    return path


REQUIRED_RUNTIME_CHECKS = {'bundle-signature', 'flatpak-install', 'flatpak-launch',
                           'flatpak-shutdown', 'host-component-install', 'host-component-verify'}


def validated_runtime_evidence(evidence, receipt):
    expected = {'schemaVersion': 1, 'version': receipt['version'], 'appId': receipt['appId'],
                'architecture': 'x86_64', 'bundleSha256': receipt['bundleSha256'],
                'archiveSha256': receipt['archiveSha256'],
                'hostComponentSha256': receipt['hostComponent']['sha256'],
                'sourceManifestSha256': receipt['sourceProvenance']['sourceManifestSha256']}
    if set(evidence) != set(expected) | {'checkedAtUtc', 'checks'} or any(evidence.get(k) != v for k, v in expected.items()):
        raise ValueError('Runtime evidence must identify these exact release assets and source manifest.')
    stamp = datetime.datetime.fromisoformat(evidence['checkedAtUtc'].replace('Z', '+00:00'))
    if stamp.utcoffset() != datetime.timedelta(0):
        raise ValueError('Runtime verification time must be UTC.')
    if not isinstance(evidence['checks'], list) or not 6 <= len(evidence['checks']) <= 100:
        raise ValueError('Runtime verification must list the required checks.')
    names = set()
    passed = set()
    for check in evidence['checks']:
        if set(check) != {'name', 'result', 'summary'}:
            raise ValueError('Each runtime check needs a name, result, and public summary.')
        if not re.fullmatch(r'[a-z][a-z0-9-]{1,79}', check['name']) or check['name'] in names:
            raise ValueError('Runtime checks need unique bounded names.')
        names.add(check['name'])
        if check['result'] not in {'passed', 'unverified', 'unsupported'}:
            raise ValueError('Resolve failed runtime checks before finalizing the release.')
        if not isinstance(check['summary'], str) or not 1 <= len(check['summary']) <= 1000:
            raise ValueError('A bounded public verification summary is required.')
        if check['result'] == 'passed':
            passed.add(check['name'])
    if not REQUIRED_RUNTIME_CHECKS.issubset(passed):
        raise ValueError('Install, launch, shutdown, signature and host-component verification must pass.')
    return evidence


def sign_checksums(output, gpg, key, assets):
    checksums = output / 'SHA256SUMS'
    checksums.write_text(''.join(digest(path) + '  ' + path.name + '\n' for path in assets), encoding='ascii')
    signature = output / 'SHA256SUMS.asc'
    subprocess.run(gpg + ['--yes', '--local-user', key, '--armor', '--detach-sign',
                          '--output', str(signature), str(checksums)], check=True)
    subprocess.run(gpg + ['--verify', str(signature), str(checksums)], check=True)


def finalize():
    parser = argparse.ArgumentParser(description='Finalize an already signed candidate after exact-asset runtime verification.')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--runtime-evidence', type=Path, required=True)
    parser.add_argument('--gpg-homedir', type=Path, required=True)
    parser.add_argument('--gpg-key', type=fingerprint, required=True)
    args = parser.parse_args(sys.argv[2:])
    output = args.output.resolve(strict=True)
    receipt_file = output / 'release-verification.json'
    receipt = json.loads(receipt_file.read_text())
    if receipt['signingFingerprint'] != args.gpg_key or receipt.get('publicationReady') is not False:
        raise ValueError('Only this key may finalize its previously unfinalized release candidate.')
    if args.runtime_evidence.stat().st_size > 128 * 1024:
        raise ValueError('Runtime verification evidence exceeds the supported limit.')
    evidence = validated_runtime_evidence(json.loads(args.runtime_evidence.read_text()), receipt)
    version = receipt['version']
    assets = [output / f'MCastStudio-{version}-x86_64.flatpak',
              output / f'MCastStudio-{version}-linux-x86_64.tar.gz',
              output / receipt['hostComponent']['package'],
              output / (receipt['hostComponent']['package'] + '.asc'),
              output / 'mcast-studio-release-key.asc', receipt_file]
    if any(path.is_symlink() or not path.is_file() for path in assets):
        raise ValueError('Release assets must be regular files.')
    if set(path.name for path in output.iterdir()) != {path.name for path in assets} | {'SHA256SUMS', 'SHA256SUMS.asc'}:
        raise ValueError('The release directory contains unapproved or missing assets.')
    gpg = ['gpg', '--batch', '--homedir', str(args.gpg_homedir.resolve(strict=True))]
    subprocess.run(gpg + ['--verify', str(output / 'SHA256SUMS.asc'), str(output / 'SHA256SUMS')], check=True)
    expected_checksums = ''.join(digest(path) + '  ' + path.name + '\n' for path in assets)
    if (output / 'SHA256SUMS').read_text(encoding='ascii') != expected_checksums:
        raise ValueError('Signed candidate assets changed after packaging.')
    if (digest(assets[0]) != receipt['bundleSha256'] or digest(assets[1]) != receipt['archiveSha256']
            or digest(assets[2]) != receipt['hostComponent']['sha256']):
        raise ValueError('Candidate asset hashes do not match runtime verification.')
    receipt.pop('installedRuntimeVerification')
    receipt['publicationReady'] = True
    receipt['runtimeVerification'] = evidence
    # Sign before replacing candidate metadata so a missing/unavailable key does
    # not leave an unverifiable candidate or prevent a later finalization.
    with tempfile.TemporaryDirectory(prefix='mcast-release-finalize-', dir=output.parent) as folder:
        staged = Path(folder)
        completed_receipt = staged / receipt_file.name
        completed_receipt.write_text(json.dumps(receipt, indent=2) + '\n', encoding='utf-8')
        sign_checksums(staged, gpg, args.gpg_key, assets[:-1] + [completed_receipt])
        completed_receipt.replace(receipt_file)
        (staged / 'SHA256SUMS.asc').replace(output / 'SHA256SUMS.asc')
        (staged / 'SHA256SUMS').replace(output / 'SHA256SUMS')
    print(json.dumps({'version': version, 'publicationReady': True,
                      'runtimeChecks': len(evidence['checks']), 'fingerprint': args.gpg_key}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--archive-receipt', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--screenshot-url', required=True)
    parser.add_argument('--host-component', type=Path, required=True)
    parser.add_argument('--gpg-homedir', type=Path, required=True)
    parser.add_argument('--gpg-key', type=fingerprint, required=True)
    parser.add_argument('--work', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if not re.fullmatch(r'[1-9]\d*\.\d+\.\d+', args.version):
        parser.error('A public release requires a stable three-component version.')
    datetime.date.fromisoformat(args.date)
    prepare_release.https_url(args.screenshot_url)
    archive = args.archive.resolve(strict=True)
    prepare_release.verify_archive(archive, args.sha256)
    archive_receipt = json.loads(args.archive_receipt.read_text())
    if (archive_receipt.get('sha256') != args.sha256.lower() or archive_receipt.get('selfContained') is not True
            or archive_receipt.get('canonicalOutputUnmodified') is not True
            or archive_receipt.get('platform') != 'org.gnome.Platform//50'):
        raise ValueError('The runtime archive needs its matching canonical preparation receipt.')
    provenance = {key: archive_receipt[key] for key in
                  ['sourceRevision', 'sourceDirty', 'sourceManifestSha256', 'sourceFilesVerified',
                   'platform', 'libcMaximumAllowed']}
    if (not re.fullmatch(r'[0-9a-f]{40}', provenance['sourceRevision'])
            or not re.fullmatch(r'[0-9a-f]{64}', provenance['sourceManifestSha256'])
            or not isinstance(provenance['sourceDirty'], bool)
            or not isinstance(provenance['sourceFilesVerified'], int) or provenance['sourceFilesVerified'] <= 0
            or provenance['libcMaximumAllowed'] != '2.42'):
        raise ValueError('Canonical source provenance is incomplete or invalid.')
    host_component = args.host_component.resolve(strict=True)
    component_validation = validate_host_component(host_component, archive, args.version)
    key_home = args.gpg_homedir.resolve(strict=True)
    work, output = empty_directory(args.work), empty_directory(args.output)
    if (work == output or work.is_relative_to(output) or output.is_relative_to(work)
            or key_home.is_relative_to(work) or key_home.is_relative_to(output)):
        parser.error('Work, output and signing-key storage must remain separate.')

    gpg = ['gpg', '--batch', '--homedir', str(key_home)]
    key_listing = subprocess.check_output(
        gpg + ['--with-colons', '--list-secret-keys', args.gpg_key], text=True)
    primary_fingerprints = []
    awaiting_primary = False
    for line in key_listing.splitlines():
        fields = line.split(':')
        if fields[0] == 'sec':
            awaiting_primary = True
        elif fields[0] == 'ssb':
            awaiting_primary = False
        elif fields[0] == 'fpr' and awaiting_primary:
            primary_fingerprints.append(fields[9].upper())
            awaiting_primary = False
    if primary_fingerprints != [args.gpg_key]:
        parser.error('The selected primary release signing key is not available.')

    work.mkdir(parents=True, exist_ok=True)
    output.mkdir(parents=True, exist_ok=True)
    public_key = output / 'mcast-studio-release-key.asc'
    exported = subprocess.check_output(gpg + ['--armor', '--export', args.gpg_key])
    if b'-----BEGIN PGP PUBLIC KEY BLOCK-----' not in exported or b'PRIVATE KEY' in exported:
        raise ValueError('A public signing key could not be exported.')
    public_key.write_bytes(exported)

    generated = work / 'generated'
    prepare_release.generate(archive, None, args.sha256, args.version, args.date,
                             args.screenshot_url, generated, local_archive=True)
    app_id = json.loads((prepare_release.ROOT / 'release.json').read_text())['app_id']
    manifest = generated / (app_id + '.json')
    subprocess.run(['desktop-file-validate', str(generated / (app_id + '.desktop'))], check=True)
    subprocess.run(['appstreamcli', 'validate', '--no-net',
                    str(generated / (app_id + '.metainfo.xml'))], check=True)
    repository = work / 'repo'
    subprocess.run(['flatpak-builder', '--user', '--install-deps-from=flathub',
                    '--disable-rofiles-fuse', '--state-dir=' + str(work / 'state'),
                    '--repo=' + str(repository), '--gpg-sign=' + args.gpg_key,
                    '--gpg-homedir=' + str(key_home), str(work / 'build'), str(manifest)], check=True)
    bundle = output / ('MCastStudio-' + args.version + '-x86_64.flatpak')
    subprocess.run(['flatpak', 'build-bundle', '--arch=x86_64',
                    '--gpg-keys=' + str(public_key),
                    '--runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo',
                    str(repository), str(bundle), app_id, 'stable'], check=True)
    if bundle.stat().st_size >= 2 * 1024**3:
        raise ValueError('The Flatpak exceeds the GitHub per-asset release size limit.')
    archive_copy = output / ('MCastStudio-' + args.version + '-linux-x86_64.tar.gz')
    shutil.copyfile(archive, archive_copy)
    if digest(archive_copy) != args.sha256.lower():
        raise ValueError('The copied release archive failed integrity verification.')
    component_copy = output / host_component.name
    shutil.copyfile(host_component, component_copy)
    if digest(component_copy) != component_validation['sha256']:
        raise ValueError('The approved host package changed while preparing the release.')
    component_signature = output / (component_copy.name + '.asc')
    subprocess.run(gpg + ['--local-user', args.gpg_key, '--armor', '--detach-sign',
                          '--output', str(component_signature), str(component_copy)], check=True)
    subprocess.run(gpg + ['--verify', str(component_signature), str(component_copy)], check=True)
    receipt = output / 'release-verification.json'
    receipt.write_text(json.dumps({
        'version': args.version, 'releaseDate': args.date, 'appId': app_id,
        'architecture': 'x86_64', 'branch': 'stable',
        'signingFingerprint': args.gpg_key,
        'archiveSha256': digest(archive_copy), 'bundleSha256': digest(bundle),
        'hostComponent': component_validation,
        'sourceProvenance': provenance, 'publicationReady': False,
        'packagingCommit': subprocess.check_output(
            ['git', '-C', str(prepare_release.ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'packagingWorkingTreeDirty': bool(subprocess.check_output(
            ['git', '-C', str(prepare_release.ROOT), 'status', '--porcelain'], text=True).strip()),
        'installedRuntimeVerification': 'Required before publication; packaging alone is not runtime verification.',
    }, indent=2) + '\n', encoding='utf-8')
    sign_checksums(output, gpg, args.gpg_key,
                   [bundle, archive_copy, component_copy, component_signature, public_key, receipt])
    print(json.dumps({'version': args.version, 'fingerprint': args.gpg_key,
                      'bundle': bundle.name, 'sha256': digest(bundle)}))


if __name__ == '__main__':
    try:
        if len(sys.argv) > 1 and sys.argv[1] == 'finalize':
            finalize()
        else:
            main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
