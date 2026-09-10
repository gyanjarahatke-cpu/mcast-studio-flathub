#!/usr/bin/env python3
"""Create a standalone Flatpak manifest from a verified MCast Linux release."""
import argparse
import datetime
import hashlib
import json
import posixpath
import re
import shutil
import tarfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 8 * 1024**3
REQUIRED = {
    'MCast', 'MCast.dll', 'MCast.deps.json', 'MCast.runtimeconfig.json',
    'MCast.Native.Runtime.so', 'MCast.Native.Automation.so', 'MCast.Native.Tools.so',
    'MCast.Camera.Native.so', 'MCast.VirtualCamera.Setup', 'libslang-compiler.so',
    'NativeFilterProviders/x64/MCast.Native.ShaderFilters.so',
    'libcoreclr.so', 'libhostfxr.so', 'libhostpolicy.so',
    'Browser/MCast.Browser.Host', 'Browser/libcef.so', 'Browser/icudtl.dat',
    'Tools/ffmpeg/ffmpeg', 'Tools/ffmpeg/ffprobe',
}


def https_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
            parsed.password or parsed.fragment or parsed.query):
        raise ValueError('Use a public HTTPS release URL without credentials, query, or fragment.')
    return value


def verify_archive(path, expected_hash):
    if not re.fullmatch(r'[0-9a-fA-F]{64}', expected_hash):
        raise ValueError('SHA-256 must contain exactly 64 hexadecimal characters.')
    if path.stat().st_size > MAX_BYTES:
        raise ValueError('Release archive exceeds the supported size limit.')
    with path.open('rb') as stream:
        actual = hashlib.file_digest(stream, 'sha256').hexdigest()
    if actual != expected_hash.lower():
        raise ValueError('Release archive SHA-256 does not match.')
    names = set()
    links = {}
    unpacked_bytes = 0
    with tarfile.open(path, 'r:*') as archive:
        for member in archive:
            name = PurePosixPath(member.name)
            if name.is_absolute() or '..' in name.parts or '\\' in member.name:
                raise ValueError('Release archive contains an unsafe path.')
            normalized = name.as_posix()
            if normalized == '.':
                continue
            if normalized in names:
                raise ValueError('Release archive contains duplicate paths.')
            names.add(normalized)
            if (set(name.parts) & {'.git', '.ssh', '__pycache__'} or
                    name.name.endswith(('.Tests', '.SmokeTests')) or
                    name.suffix.lower() in {'.pdb', '.pfx', '.key', '.cs', '.cpp', '.vcxproj'}):
                raise ValueError('Release archive contains development or private files.')
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                raise ValueError('Release archive contains a special device entry.')
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                combined = (name.parent / link).as_posix() if member.issym() else link.as_posix()
                target = posixpath.normpath(combined)
                if link.is_absolute() or '\\' in member.linkname or target == '..' or target.startswith('../'):
                    raise ValueError('Release archive contains an escaping link.')
                links[normalized] = target
            unpacked_bytes += member.size
            if unpacked_bytes > MAX_BYTES:
                raise ValueError('Unpacked release exceeds the supported size limit.')
            if normalized == 'MCast':
                if not member.isfile() or not member.mode & 0o111:
                    raise ValueError('MCast must be a regular executable.')
                with archive.extractfile(member) as executable:
                    header = executable.read(20)
                if len(header) < 20 or header[:6] != b'\x7fELF\x02\x01' or header[18:20] != b'\x3e\x00':
                    raise ValueError('MCast must be a Linux x86_64 executable.')
    for link_name in links:
        target = link_name
        visited = set()
        while True:
            parts = PurePosixPath(target).parts
            prefix = next(('/'.join(parts[:i]) for i in range(1, len(parts) + 1)
                           if '/'.join(parts[:i]) in links), None)
            if prefix is None:
                break
            if prefix in visited:
                raise ValueError('Release archive contains a cyclic link.')
            visited.add(prefix)
            suffix = target[len(prefix):].lstrip('/')
            target = posixpath.normpath(posixpath.join(links[prefix], suffix))
        if target not in names:
            raise ValueError('Release archive contains a missing link target.')
    missing = sorted(REQUIRED - names)
    if missing or not any(n.startswith('Resources/') for n in names):
        raise ValueError('Release is not a complete self-contained Linux payload: ' + ', '.join(missing))
    return actual


def generate(archive, url, sha256, version, release_date, screenshot_url, output, *, local_archive=False):
    if local_archive:
        if url is not None:
            raise ValueError('Local packaging must not specify a release URL.')
    else:
        https_url(url)
        if not screenshot_url:
            raise ValueError('Public release preparation requires a real screenshot URL.')
    if screenshot_url:
        https_url(screenshot_url)
    if not re.fullmatch(r'[1-9]\d*\.\d+\.\d+(?:-(?:beta|rc)\.\d+)?', version):
        raise ValueError('Use a release version such as 1.0.0-beta.1, 1.0.0-rc.1, or 1.0.0.')
    datetime.date.fromisoformat(release_date)
    digest = verify_archive(archive, sha256)
    config = json.loads((ROOT / 'release.json').read_text())
    app_id = config['app_id']
    branch = 'beta' if '-' in version else 'stable'
    output.mkdir(parents=True, exist_ok=True)
    for source in (ROOT / 'metadata').iterdir():
        if source.name == 'mcast-studio' or source.suffix == '.desktop':
            (output / source.name).write_bytes(source.read_text(encoding='utf-8').encode('utf-8'))
        else:
            shutil.copyfile(source, output / source.name)

    component = ET.Element('component', type='desktop-application')
    for key, value in [('id', app_id), ('metadata_license', 'CC0-1.0'),
                       ('project_license', 'LicenseRef-proprietary'), ('name', 'MCast Studio'),
                       ('summary', 'Create live productions, recordings, and broadcasts')]:
        ET.SubElement(component, key).text = value
    developer = ET.SubElement(component, 'developer', id='com.mcaststudio')
    ET.SubElement(developer, 'name').text = 'MCast Studio'
    ET.SubElement(component, 'launchable', type='desktop-id').text = app_id + '.desktop'
    description = ET.SubElement(component, 'description')
    for paragraph in [
        'MCast Studio combines scenes, cameras, media, graphics, audio, remote guests, recording, and streaming in a live production workspace.',
        'An MCast account and a valid application license are required. Visit the website for available plans.',
    ]:
        ET.SubElement(description, 'p').text = paragraph
    for kind, suffix in [('homepage', '/'), ('help', '/support/'), ('contact', '/contact/'), ('vcs-browser', '')]:
        ET.SubElement(component, 'url', type=kind).text = (
            'https://github.com/gyanjarahatke-cpu/mcast-studio-flathub' if kind == 'vcs-browser'
            else 'https://mcaststudio.com' + suffix)
    ET.SubElement(component, 'url', type='bugtracker').text = 'https://github.com/gyanjarahatke-cpu/mcast-studio-flathub/issues'
    rating = ET.SubElement(component, 'content_rating', type='oars-1.1')
    for attribute in ['social-chat', 'social-audio']:
        ET.SubElement(rating, 'content_attribute', id=attribute).text = 'intense'
    if screenshot_url:
        screenshots = ET.SubElement(component, 'screenshots')
        shot = ET.SubElement(screenshots, 'screenshot', type='default')
        ET.SubElement(shot, 'image').text = screenshot_url
    release = ET.SubElement(ET.SubElement(component, 'releases'), 'release',
                            version=version, date=release_date,
                            type='development' if branch == 'beta' else 'stable')
    ET.SubElement(ET.SubElement(release, 'description'), 'p').text = 'MCast Studio ' + version + ' release.'
    ET.indent(component)
    ET.ElementTree(component).write(output / (app_id + '.metainfo.xml'), encoding='utf-8', xml_declaration=True)

    archive_source = {'type': 'archive', 'sha256': digest, 'strip-components': 0, 'dest': 'payload'}
    archive_source['path' if local_archive else 'url'] = str(archive.resolve()) if local_archive else url
    manifest = {
        'app-id': app_id, 'runtime': config['runtime'], 'runtime-version': config['runtime_version'],
        'sdk': config['sdk'], 'command': 'mcast-studio', 'default-branch': branch,
        'separate-locales': False,
        'finish-args': ['--share=network', '--share=ipc', '--socket=x11', '--socket=wayland',
                        '--socket=pulseaudio', '--device=all', '--filesystem=xdg-run/pipewire-0',
                        '--filesystem=xdg-videos', '--filesystem=xdg-pictures:ro',
                        '--filesystem=xdg-music:ro', '--talk-name=org.freedesktop.secrets'],
        'modules': [{
            'name': 'mcast-studio', 'buildsystem': 'simple',
            'build-commands': [
                'mkdir -p /app/lib/mcast', 'cp -a payload/. /app/lib/mcast/',
                'install -Dm755 mcast-studio /app/bin/mcast-studio',
                f'install -Dm644 {app_id}.desktop /app/share/applications/{app_id}.desktop',
                f'install -Dm644 {app_id}.metainfo.xml /app/share/metainfo/{app_id}.metainfo.xml',
                f'install -Dm644 {app_id}.png /app/share/icons/hicolor/512x512/apps/{app_id}.png',
            ],
            'sources': [archive_source] +
                       [{'type': 'file', 'path': n} for n in ['mcast-studio', app_id + '.desktop', app_id + '.metainfo.xml', app_id + '.png']],
        }],
    }
    (output / (app_id + '.json')).write_text(json.dumps(manifest, indent=2) + '\n')
    (output / 'flathub.json').write_text(json.dumps({'only-arches': [config['architecture']]}, indent=2) + '\n')
    return branch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--url')
    source.add_argument('--local-archive', action='store_true', help='Build an unpublished installation candidate from a local verified archive.')
    parser.add_argument('--sha256', required=True)
    parser.add_argument('--version', default=json.loads((ROOT / 'release.json').read_text())['version'])
    parser.add_argument('--date', required=True)
    parser.add_argument('--screenshot-url')
    parser.add_argument('--output', type=Path, default=ROOT / 'generated')
    parser.add_argument('--download', action='store_true')
    args = parser.parse_args()
    if args.download and args.local_archive:
        parser.error('--download requires --url.')
    if args.download:
        https_url(args.url)
        with urllib.request.urlopen(args.url, timeout=120) as response, args.archive.open('wb') as out:
            redirect = urllib.parse.urlsplit(response.geturl())
            if redirect.scheme != 'https' or not redirect.hostname or redirect.username or redirect.password:
                raise ValueError('Release download redirected to an insecure location.')
            total = 0
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_BYTES:
                    raise ValueError('Release download exceeds the supported size limit.')
                out.write(chunk)
    branch = generate(args.archive, args.url, args.sha256, args.version, args.date, args.screenshot_url, args.output,
                      local_archive=args.local_archive)
    print('Prepared verified manifest on branch ' + branch)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, tarfile.TarError) as error:
        raise SystemExit(str(error))
