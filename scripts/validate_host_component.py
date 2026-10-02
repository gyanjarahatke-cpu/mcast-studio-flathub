"""Validate the exact architecture-independent camera deployment package.

Only the fixed package identity, dependencies and package-owned scripts/assets
already verified in the application runtime archive are accepted for signing.
"""
import hashlib
import subprocess
import tarfile
from pathlib import PurePosixPath

ASSETS = {
    'usr/libexec/mcast/virtual-camera-host-setup': ('virtual-camera-host-setup', 0o755),
    'lib/systemd/system/mcast-virtual-camera.service': ('mcast-virtual-camera.service', 0o644),
    'lib/udev/rules.d/70-mcast-virtual-camera.rules': ('70-mcast-virtual-camera.rules', 0o644),
    'usr/share/mcast/virtual-camera/cameras.txt': ('cameras.txt', 0o644),
}
CONTROL_SCRIPTS = {'postinst', 'prerm', 'postrm'}
DEPENDENCIES = {
    'v4l2loopback-dkms (>= 0.15.0)', 'v4l2loopback-utils (>= 0.15.0)',
    'v4l-utils', 'kmod', 'udev', 'systemd', 'util-linux', 'coreutils',
}
PREFIX = 'Tools/virtual-camera/linux/'
MAX_PACKAGE = 8 * 1024**2
MAX_EXPANDED = 32 * 1024**2


def canonical_assets(archive):
    wanted = {name for name, _ in ASSETS.values()} | CONTROL_SCRIPTS
    result = {}
    with tarfile.open(archive, 'r:*') as source:
        for member in source:
            name = PurePosixPath(member.name).as_posix()
            if not name.startswith(PREFIX) or name[len(PREFIX):] not in wanted:
                continue
            relative = name[len(PREFIX):]
            if not member.isfile() or member.size > MAX_PACKAGE or relative in result:
                raise ValueError('The canonical host-component payload is invalid.')
            result[relative] = source.extractfile(member).read()
    if set(result) != wanted:
        raise ValueError('The runtime archive is missing the approved host camera deployment assets.')
    return result


def read_package_members(stream, allowed):
    result = {}
    total = 0
    allowed_directories = {'.'}
    for name in allowed:
        allowed_directories.update(parent.as_posix() for parent in PurePosixPath(name).parents)
    for member in stream:
        name = PurePosixPath(member.name)
        if name.is_absolute() or '..' in name.parts or '\\' in member.name:
            raise ValueError('The host package contains an unsafe path.')
        normalized = name.as_posix()
        if member.uid != 0 or member.gid != 0:
            raise ValueError('Host package assets must be owned by root.')
        if member.isdir():
            if normalized not in allowed_directories or member.mode & 0o7777 != 0o755:
                raise ValueError('The host package contains an unexpected directory.')
            continue
        if normalized not in allowed or normalized in result or not member.isfile():
            raise ValueError('The host package contains an unexpected payload or link.')
        total += member.size
        if member.size > MAX_PACKAGE or total > MAX_EXPANDED:
            raise ValueError('The host package exceeds the supported size limit.')
        if member.mode & 0o7777 != allowed[normalized]:
            raise ValueError('The host package contains an unsafe file mode.')
        result[normalized] = stream.extractfile(member).read()
    if set(result) != set(allowed):
        raise ValueError('The host package is incomplete.')
    return result


def read_deb_tar(package, kind, allowed):
    process = subprocess.Popen(['dpkg-deb', kind, str(package)], stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    try:
        with tarfile.open(fileobj=process.stdout, mode='r|') as stream:
            result = read_package_members(stream, allowed)
        _, stderr = process.communicate(timeout=30)
        if process.returncode:
            raise ValueError('The host package could not be inspected: ' + stderr.decode(errors='replace'))
    except BaseException:
        process.kill()
        process.communicate()
        raise
    return result


def control_fields(content):
    fields = {}
    key = None
    for line in content.decode('utf-8').splitlines():
        if line.startswith(' ') and key:
            fields[key] += '\n' + line
        else:
            key, separator, value = line.partition(':')
            if not separator or key in fields:
                raise ValueError('The host package control metadata is malformed.')
            fields[key] = value.strip()
    return fields


def validate_contents(data, control, canonical, version):
    if set(data) != set(ASSETS) or set(control) != CONTROL_SCRIPTS | {'control'}:
        raise ValueError('The host package contains an unexpected or missing asset.')
    for name, content in data.items():
        if content != canonical[ASSETS[name][0]]:
            raise ValueError('The host package differs from the verified application deployment assets.')
    for name in CONTROL_SCRIPTS:
        if control[name] != canonical[name]:
            raise ValueError('The host package has an unapproved installation script.')
    fields = control_fields(control['control'])
    fixed = {'Package': 'mcast-virtual-camera', 'Version': version, 'Architecture': 'all',
             'Section': 'video', 'Priority': 'optional',
             'Maintainer': 'MCast Studio <support@mcaststudio.com>',
             'Homepage': 'https://mcaststudio.com/download/linux/'}
    if set(fields) != set(fixed) | {'Depends', 'Description'} or any(fields.get(k) != v for k, v in fixed.items()):
        raise ValueError('The host package identity or control metadata does not match this release.')
    dependencies = [item.strip() for item in fields['Depends'].split(',')]
    if len(dependencies) != len(DEPENDENCIES) or set(dependencies) != DEPENDENCIES:
        raise ValueError('The host package requests unapproved system dependencies.')
    if not fields['Description'].startswith('MCast virtual camera system setup\n'):
        raise ValueError('The host package description does not match its purpose.')


def validate_host_component(package, archive, version):
    if package.name != f'mcast-virtual-camera_{version}_all.deb' or package.stat().st_size > MAX_PACKAGE:
        raise ValueError('Use the matching architecture-independent MCast camera package.')
    original_digest = hashlib.sha256(package.read_bytes()).hexdigest()
    canonical = canonical_assets(archive)
    data = read_deb_tar(package, '--fsys-tarfile', {name: mode for name, (_, mode) in ASSETS.items()})
    control = read_deb_tar(package, '--ctrl-tarfile', {'control': 0o644, **{name: 0o755 for name in CONTROL_SCRIPTS}})
    validate_contents(data, control, canonical, version)
    if hashlib.sha256(package.read_bytes()).hexdigest() != original_digest:
        raise ValueError('The host package changed during verification.')
    return {'package': package.name, 'version': version, 'architecture': 'all',
            'sha256': original_digest,
            'approvedPayloadFiles': sorted(data), 'approvedControlScripts': sorted(CONTROL_SCRIPTS)}
