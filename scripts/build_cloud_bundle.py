"""Build an allowlisted upload folder. Never uploads or changes the source DB."""
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from database import seed_draws

FILES = (
    'app.py', 'collector.py', 'database.py', 'filters.py', 'engine.py', 'validator.py',
    'settings.py', 'backtesting.py', 'experiments.py', 'analysis_ui.py',
    'runtime_support.py', 'personal_backup.py', 'requirements.txt', 'requirements-dev.txt',
    'README.md', 'DEPLOYMENT.md', '.gitignore', '.streamlit/config.toml',
    'seed/draws.json', 'data/.gitkeep',
    'seed/draws-001247.json',
)


def build(destination=None):
    destination = Path(destination) if destination else ROOT / 'dist' / 'lotto-lab-cloud'
    if destination.exists():
        # Never recursively delete existing directories or leave stale secrets in a bundle.
        raise ValueError(f'대상 폴더가 이미 있습니다. 새 경로를 지정하세요: {destination}')
    seed_draws()
    destination.mkdir(parents=True)
    manifest = {}
    for name in FILES:
        source = ROOT / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        # The allowlist contains text files. Match GitHub/Linux line endings
        # even when Git for Windows checked the source out with CRLF.
        target.write_bytes(source.read_bytes().replace(b'\r\n', b'\n'))
        manifest[name] = hashlib.sha256(target.read_bytes()).hexdigest()
    (destination / 'bundle-manifest.json').write_text(
        json.dumps(manifest, indent=2), encoding='utf-8', newline='\n')
    return destination


if __name__ == '__main__':
    print(build(Path(sys.argv[1]) if len(sys.argv) > 1 else None))
