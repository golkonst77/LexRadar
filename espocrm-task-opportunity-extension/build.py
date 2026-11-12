"""Package only this extension; no repository data, secrets, or remote access."""
from pathlib import Path
import hashlib
import tarfile

root = Path(__file__).resolve().parent
files = sorted(p for p in root.rglob('*') if p.is_file()
               and p.relative_to(root).parts[0] != 'dist'
               and '__pycache__' not in p.parts and p.name != 'FILES.sha256')
manifest = root / 'FILES.sha256'
manifest.write_text(''.join(f'{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(root)}\n' for p in files))
files.append(manifest)
out = root / 'dist'
out.mkdir(exist_ok=True)
archive = out / 'lexradar-task-opportunity-1.0.0.tar.gz'
with tarfile.open(archive, 'w:gz') as tf:
    for p in sorted(files):
        tf.add(p, arcname=f'{root.name}/{p.relative_to(root)}', recursive=False)
(out/'SHA256SUMS').write_text(f'{hashlib.sha256(archive.read_bytes()).hexdigest()}  dist/{archive.name}\n')
print(archive)
