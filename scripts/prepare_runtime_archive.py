#!/usr/bin/env python3
"""Seal the canonical Linux Release payload into a portable deployment archive.

Only a deployment staging copy is normalized. The canonical build output is
never modified, rebuilt, or launched here.
"""
import argparse
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path, PurePosixPath

import prepare_release

RPATHS = {
    'MCast.Native.Runtime.so': '$ORIGIN:$ORIGIN/Tools/ffmpeg:$ORIGIN/Tools/ndi',
    'MCast.Native.Automation.so': '$ORIGIN:$ORIGIN/Tools/ffmpeg:$ORIGIN/Tools/ndi',
    'MCast.Native.Tools.so': '$ORIGIN:$ORIGIN/Tools/ffmpeg:$ORIGIN/Tools/ndi',
    'NativeFilterProviders/x64/MCast.Native.ShaderFilters.so': '$ORIGIN/../..',
    'Browser/MCast.Browser.Host': '$ORIGIN',
}
DEBUG_SUFFIXES = {'.pdb', '.dbg', '.mdb', '.dSYM', '.ilk', '.lib', '.exp', '.iobj', '.ipdb'}
PRIVATE_SUFFIXES = {'.key', '.pfx', '.p12', '.pem', '.cs', '.cpp', '.vcxproj', '.csproj', '.dmp'}
PRIVATE_DIRECTORIES = {'.git', '.ssh', '.gnupg', '.codex-run', '__pycache__', '.pytest_cache'}
ASSET_LIMIT = 2 * 1024**3


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def development_file(path):
    name = path.name
    return (path.suffix in DEBUG_SUFFIXES or
            (name.startswith('MCast.') and bool(re.search(r'(?:Tests|CrashProbe)(?:\.|$)', name))) or
            name.startswith(('MCast.Camera.Consumer', 'MCast.NdiBridge.Consumer', 'MCast.VirtualCamera.Consumer')))


def validate_canonical(path):
    path = path.resolve(strict=True)
    if tuple(path.parts[-5:]) != ('src', 'MCastStudio', 'bin', 'linux-x64', 'Release'):
        raise ValueError('Use the canonical src/MCastStudio/bin/linux-x64/Release output.')
    if not (path / 'MCast').is_file():
        raise ValueError('The canonical Release application is missing.')
    return path


def assert_stopped(canonical):
    for process in Path('/proc').iterdir():
        if not process.name.isdigit():
            continue
        try:
            executable = (process / 'exe').resolve(strict=True)
        except (FileNotFoundError, PermissionError, OSError):
            continue
        if executable.is_relative_to(canonical):
            raise ValueError('Stop the canonical development application and its helpers before packaging.')


def verify_source_manifest(manifest_path, repository):
    data = json.loads(manifest_path.read_text())
    if not re.fullmatch(r'[0-9a-f]{40}', data.get('revision', '')) or not data.get('expected'):
        raise ValueError('The source manifest must contain an exact Git revision and source hashes.')
    for name, expected in data['expected'].items():
        relative = PurePosixPath(name)
        if relative.is_absolute() or '..' in relative.parts or '\\' in name:
            raise ValueError('The source manifest contains an unsafe relative path.')
        source = repository.joinpath(*relative.parts).resolve(strict=True)
        if not source.is_relative_to(repository) or not re.fullmatch(r'[0-9a-f]{64}', expected):
            raise ValueError('The source manifest contains an invalid source entry.')
        if sha256(source) != expected:
            raise ValueError('The source tree changed since its verified manifest: ' + name)
    return data


def validate_elf(path, stage):
    with path.open('rb') as stream:
        header = stream.read(20)
    if header[:4] != b'\x7fELF':
        return None
    name = path.relative_to(stage).as_posix()
    if header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
        raise ValueError('The Linux x86_64 payload contains an incompatible ELF: ' + name)
    symbols = subprocess.check_output(['readelf', '-W', '--dyn-syms', str(path)], text=True)
    required = []
    for line in symbols.splitlines():
        if ' UND ' not in line:
            continue
        if 'GLIBC_PRIVATE' in line:
            raise ValueError('A private libc ABI entered the release: ' + name)
        for major, minor in re.findall(r'@GLIBC_(\d+)\.(\d+)', line):
            required.append((int(major), int(minor)))
    if required and max(required) > (2, 42):
        raise ValueError('A native binary requires libc newer than GNOME 50: ' + name)
    dynamic = subprocess.check_output(['readelf', '-W', '-d', str(path)], text=True)
    needed = re.findall(r'\(NEEDED\).*?\[(.*?)\]', dynamic)
    for dependency in needed:
        if '/' in dependency:
            raise ValueError('An absolute native dependency entered the release: ' + name)
    for value in re.findall(r'\((?:RPATH|RUNPATH)\).*?\[(.*?)\]', dynamic):
        for entry in value.split(':'):
            if not (entry == '$ORIGIN' or entry.startswith('$ORIGIN/')):
                raise ValueError('A non-package-relative library path entered the release: ' + name)
            resolved = Path(entry.replace('$ORIGIN', str(path.parent))).resolve()
            if not resolved.is_relative_to(stage):
                raise ValueError('A library path escapes the package: ' + name)
    if needed:
        resolution = subprocess.run(
            ['flatpak', 'run', '--unshare=network', '--filesystem=' + str(stage) + ':ro',
             '--command=ldd', 'org.gnome.Platform//50', str(path)],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        if resolution.returncode or 'not found' in resolution.stdout:
            raise ValueError('A native dependency is unavailable in GNOME Platform 50: ' + name + '\n' + resolution.stdout)
    return {'path': name, 'sha256': sha256(path), 'needed': needed,
            'maximumGlibc': '.'.join(map(str, max(required))) if required else None}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--canonical', type=Path, required=True)
    parser.add_argument('--stage', type=Path, required=True)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--receipt', type=Path, required=True)
    parser.add_argument('--source-manifest', type=Path, required=True)
    parser.add_argument('--epoch', type=int, required=True)
    args = parser.parse_args()
    canonical = validate_canonical(args.canonical)
    assert_stopped(canonical)
    source_manifest_path = args.source_manifest.resolve(strict=True)
    source_manifest_hash = sha256(source_manifest_path)
    source_manifest = verify_source_manifest(source_manifest_path, canonical.parents[4])
    stage, archive, receipt = args.stage.resolve(), args.archive.resolve(), args.receipt.resolve()
    if (archive == receipt or stage == canonical or stage.is_relative_to(canonical) or canonical.is_relative_to(stage)
            or archive.is_relative_to(stage) or archive.is_relative_to(canonical)
            or receipt.is_relative_to(stage) or receipt.is_relative_to(canonical)):
        parser.error('Canonical output, deployment stage, archive and receipt must be separate.')
    if stage.exists() or archive.exists() or receipt.exists():
        parser.error('Use new staging, archive and receipt paths; existing releases are never overwritten.')
    if args.epoch < 0:
        parser.error('The archive timestamp must be non-negative.')
    runtime = json.loads((canonical / 'MCast.runtimeconfig.json').read_text())['runtimeOptions']
    if runtime.get('framework') or runtime.get('frameworks') or not runtime.get('includedFrameworks'):
        parser.error('Publish the canonical Release as self-contained before packaging.')
    source_hashes, excluded, entries = {}, [], []
    for path in sorted(canonical.rglob('*')):
        relative = path.relative_to(canonical)
        if set(relative.parts) & PRIVATE_DIRECTORIES or path.suffix.lower() in PRIVATE_SUFFIXES:
            raise ValueError('A private or source file entered the canonical payload: ' + relative.as_posix())
        if development_file(relative):
            excluded.append(relative.as_posix())
            continue
        if path.is_symlink():
            target = path.resolve(strict=True)
            if not target.is_relative_to(canonical) or os.path.isabs(os.readlink(path)):
                raise ValueError('A runtime symlink escapes the canonical payload: ' + relative.as_posix())
        elif path.is_file():
            source_hashes[relative.as_posix()] = sha256(path)
        elif not path.is_dir():
            raise ValueError('A special filesystem entry entered the canonical payload.')
        entries.append(relative)
    stage.mkdir(parents=True)
    for relative in entries:
        source, target = canonical / relative, stage / relative
        if source.is_symlink():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.symlink_to(os.readlink(source))
        elif source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if sha256(target) != source_hashes[relative.as_posix()]:
                raise ValueError('The canonical payload changed during deployment staging.')
    for name, value in RPATHS.items():
        subprocess.run(['patchelf', '--set-rpath', value, str(stage / name)], check=True)
    checked = [result for path in sorted(stage.rglob('*')) if path.is_file() and not path.is_symlink()
               if (result := validate_elf(path, stage)) is not None]
    for name, digest in source_hashes.items():
        if sha256(canonical / name) != digest:
            raise ValueError('The canonical Release changed during packaging: ' + name)
    verify_source_manifest(source_manifest_path, canonical.parents[4])
    if sha256(source_manifest_path) != source_manifest_hash:
        raise ValueError('The verified source manifest changed during packaging.')
    archive.parent.mkdir(parents=True, exist_ok=True)
    with archive.open('xb') as destination, gzip.GzipFile(filename='', mode='wb', fileobj=destination,
                                                         mtime=args.epoch) as compressed:
        with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as output:
            for path in sorted(stage.rglob('*')):
                info = output.gettarinfo(str(path), arcname=path.relative_to(stage).as_posix())
                info.uid = info.gid = 0
                info.uname = info.gname = 'root'
                info.mtime = args.epoch
                info.mode &= 0o777
                if path.is_file() and not path.is_symlink():
                    with path.open('rb') as stream:
                        output.addfile(info, stream)
                else:
                    output.addfile(info)
    if archive.stat().st_size >= ASSET_LIMIT:
        raise ValueError('The archive exceeds the GitHub per-asset release size limit.')
    archive_hash = sha256(archive)
    prepare_release.verify_archive(archive, archive_hash)
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({'archive': archive.name, 'sha256': archive_hash,
                                  'bytes': archive.stat().st_size, 'selfContained': True,
                                  'libcMaximumAllowed': '2.42', 'platform': 'org.gnome.Platform//50',
                                  'canonicalSourceHashes': source_hashes, 'excludedDevelopmentFiles': excluded,
                                  'sourceRevision': source_manifest['revision'], 'sourceDirty': True,
                                  'sourceManifestSha256': source_manifest_hash,
                                  'sourceFilesVerified': len(source_manifest['expected']),
                                  'elfFiles': checked, 'normalizedRpaths': RPATHS,
                                  'canonicalOutputUnmodified': True}, indent=2) + '\n')
    print(json.dumps({'archive': archive.name, 'sha256': archive_hash,
                      'bytes': archive.stat().st_size, 'elfFilesChecked': len(checked)}))


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error))
