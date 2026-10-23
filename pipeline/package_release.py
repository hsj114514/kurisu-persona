#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
"""Build a source-only ZIP from an explicit, reviewable list of files."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

ROOT = Path(__file__).resolve().parent.parent


def source_files(root: Path = ROOT) -> list[str]:
    names = json.loads((root / 'release-files.json').read_text('utf-8'))
    if not isinstance(names, list) or len(names) != len(set(names)):
        raise ValueError('release-files.json must be a unique list')
    for name in names:
        p = PurePosixPath(name)
        if (p.is_absolute() or '..' in p.parts or '\\' in name or ':' in name
                or name == 'SHA256SUMS.txt' or not (root / name).is_file()
                or (root / name).is_symlink()
                or not (root / name).resolve().is_relative_to(root.resolve())):
            raise ValueError(f'Invalid source file: {name}')
    return sorted(names)


def package(out: Path, root: Path = ROOT) -> dict:
    version = (root / 'VERSION').read_text('utf-8').strip()
    prefix = f'kurisu-persona-v{version}/'
    entries = [(name, (root / name).read_bytes()) for name in source_files(root)]
    checksums = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in entries)
    entries.append(('SHA256SUMS.txt', checksums.encode('utf-8')))
    out.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(out, 'w', compression=ZIP_DEFLATED) as z:
        for name, data in entries:
            info = ZipInfo(prefix + name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            z.writestr(info, data)
    with ZipFile(out) as z:
        assert len(z.namelist()) == len(set(z.namelist()))
        assert z.testzip() is None
        for line in checksums.splitlines():
            expected, name = line.split('  ', 1)
            assert hashlib.sha256(z.read(prefix + name)).hexdigest() == expected
    return {'archive': str(out), 'files': len(entries),
            'sha256': hashlib.sha256(out.read_bytes()).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--out', type=Path, default=ROOT / 'dist' / 'kurisu-persona-v0.1.zip')
    args = ap.parse_args()
    print(json.dumps(package(args.out), ensure_ascii=True, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
