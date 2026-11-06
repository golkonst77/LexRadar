"""Read backup integrity and inventory only; never extract, restore, or start services."""
import argparse
import gzip
import hashlib
import json
from pathlib import Path, PurePosixPath
import subprocess
import tarfile
import zlib


def inspect(path):
    path = Path(path)
    result = {'archive': path.name, 'bytes': path.stat().st_size}
    with path.open('rb') as stream:
        result['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
    if path.suffix == '.bundle':
        checked = subprocess.run(['git', 'bundle', 'verify', str(path.resolve())],
                                 capture_output=True, timeout=60)
        result.update(kind='bundle', verified=checked.returncode == 0)
        return result
    if not path.name.endswith(('.tar.gz', '.tgz')):
        raise ValueError('Expected .bundle, .tar.gz or .tgz')
    names = set()
    members = unsafe = links = 0
    with gzip.open(path, 'rb') as stream:
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for entry in archive:
                members += 1
                name = PurePosixPath(entry.name)
                unsafe += name.is_absolute() or '..' in name.parts
                links += entry.issym() or entry.islnk()
                if entry.isfile():
                    names.add(str(name))
        # Read through the gzip trailer to validate CRC even after tar end markers.
        while stream.read(1024 * 1024):
            pass
    targets = ('control_mvp.py', 'control_mailing.py', 'control_followup.py',
               'jobs.sqlite3', 'mailing.json', 'followup.sqlite3',
               'crm-prepare.json', 'espo-export.json', 'config.json',
               'batch.json', 'approved.json', 'deals-state.json',
               'mailing-permissions.json', 'mailing-selection.json')
    present = {target: any(PurePosixPath(n).name == target for n in names) for target in targets}
    result.update(kind='tar.gz', verified=unsafe == 0, gzip_crc=True,
                  members=members, unsafe_paths=unsafe, links=links, present=present,
                  review_files=sum(n.endswith('/review.json') for n in names),
                  results_files=sum(n.endswith('/results.csv') for n in names),
                  proposal_pdfs=sum(n.endswith('/proposal.pdf') for n in names))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archives', nargs='+', type=Path)
    args = parser.parse_args()
    failed = False
    for path in args.archives:
        try:
            result = inspect(path)
        except (OSError, ValueError, EOFError, tarfile.TarError, zlib.error, subprocess.SubprocessError):
            result = {'archive': path.name, 'verified': False, 'error': 'unreadable_or_invalid'}
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        failed |= not result['verified']
    return int(failed)


if __name__ == '__main__':
    raise SystemExit(main())
