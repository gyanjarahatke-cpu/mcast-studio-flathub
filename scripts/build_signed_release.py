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
from pathlib import Path

import prepare_release


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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--version', required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--screenshot-url', required=True)
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
    archive_copy = output / ('MCastStudio-' + args.version + '-linux-x86_64.tar.gz')
    shutil.copyfile(archive, archive_copy)
    if digest(archive_copy) != args.sha256.lower():
        raise ValueError('The copied release archive failed integrity verification.')
    receipt = output / 'release-verification.json'
    receipt.write_text(json.dumps({
        'version': args.version, 'releaseDate': args.date, 'appId': app_id,
        'architecture': 'x86_64', 'branch': 'stable',
        'signingFingerprint': args.gpg_key,
        'archiveSha256': digest(archive_copy), 'bundleSha256': digest(bundle),
        'packagingCommit': subprocess.check_output(
            ['git', '-C', str(prepare_release.ROOT), 'rev-parse', 'HEAD'], text=True).strip(),
        'packagingWorkingTreeDirty': bool(subprocess.check_output(
            ['git', '-C', str(prepare_release.ROOT), 'status', '--porcelain'], text=True).strip()),
        'installedRuntimeVerification': 'Required before publication; packaging alone is not runtime verification.',
    }, indent=2) + '\n', encoding='utf-8')
    checksums = output / 'SHA256SUMS'
    checksums.write_text(''.join(digest(path) + '  ' + path.name + '\n'
                                for path in [bundle, archive_copy, public_key, receipt]), encoding='ascii')
    signature = output / 'SHA256SUMS.asc'
    subprocess.run(gpg + ['--local-user', args.gpg_key, '--armor', '--detach-sign',
                          '--output', str(signature), str(checksums)], check=True)
    subprocess.run(gpg + ['--verify', str(signature), str(checksums)], check=True)
    print(json.dumps({'version': args.version, 'fingerprint': args.gpg_key,
                      'bundle': bundle.name, 'sha256': digest(bundle)}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
