import copy
import io
import sys
import tarfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import validate_host_component as host


class HostPackageSafetyTests(unittest.TestCase):
    def contents(self):
        canonical = {name: name.encode() for name, _ in host.ASSETS.values()}
        canonical.update({name: ('#!/bin/sh\n' + name).encode() for name in host.CONTROL_SCRIPTS})
        data = {name: canonical[source] for name, (source, _) in host.ASSETS.items()}
        fields = {'Package': 'mcast-virtual-camera', 'Version': '1.0.40', 'Architecture': 'all',
                  'Section': 'video', 'Priority': 'optional',
                  'Maintainer': 'MCast Studio <support@mcaststudio.com>',
                  'Homepage': 'https://mcaststudio.com/download/linux/',
                  'Depends': ', '.join(sorted(host.DEPENDENCIES)),
                  'Description': 'MCast virtual camera system setup\n Host camera deployment.'}
        control = {name: canonical[name] for name in host.CONTROL_SCRIPTS}
        control['control'] = ''.join(f'{key}: {value}\n' for key, value in fields.items()).encode()
        return data, control, canonical

    def test_only_matching_canonical_payload_and_metadata_can_be_signed(self):
        data, control, canonical = self.contents()
        host.validate_contents(data, control, canonical, '1.0.40')
        for original, replacement in [(b'Architecture: all', b'Architecture: amd64'),
                                      (b'Version: 1.0.40', b'Version: 1.0.39'),
                                      (b'Depends:', b'Depends: arbitrary-root-tool,')]:
            altered = copy.deepcopy(control)
            altered['control'] = altered['control'].replace(original, replacement)
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                host.validate_contents(data, altered, canonical, '1.0.40')
        altered = copy.deepcopy(control)
        altered['postinst'] += b'\necho unrelated command'
        with self.assertRaisesRegex(ValueError, 'unapproved installation script'):
            host.validate_contents(data, altered, canonical, '1.0.40')
        changed = copy.deepcopy(data)
        changed[next(iter(changed))] += b'changed'
        with self.assertRaisesRegex(ValueError, 'differs'):
            host.validate_contents(changed, control, canonical, '1.0.40')

    def inspect(self, member, data=b''):
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode='w') as archive:
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
        output.seek(0)
        with tarfile.open(fileobj=output, mode='r|') as archive:
            return host.read_package_members(archive, {'control': 0o644})

    def test_tar_members_refuse_extra_files_links_unsafe_paths_and_modes(self):
        valid = tarfile.TarInfo('./control')
        valid.mode = 0o644
        self.assertEqual(self.inspect(valid, b'approved'), {'control': b'approved'})
        for name in ['../control', '/control', 'unexpected', 'folder\\control']:
            member = tarfile.TarInfo(name)
            member.mode = 0o644
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.inspect(member)
        for field, value in [('uid', 1000), ('gid', 1000), ('mode', 0o664), ('mode', 0o4644),
                             ('type', tarfile.SYMTYPE), ('type', tarfile.LNKTYPE)]:
            member = tarfile.TarInfo('control')
            member.mode = 0o644
            setattr(member, field, value)
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.inspect(member)

    def test_control_parser_rejects_duplicate_identity(self):
        with self.assertRaisesRegex(ValueError, 'malformed'):
            host.control_fields(b'Package: mcast-virtual-camera\nPackage: other\n')


if __name__ == '__main__':
    unittest.main()
