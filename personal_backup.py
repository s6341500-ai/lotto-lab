"""Explicit, portable backups of personal JSON files; never modifies the draw DB."""
import json
import tempfile
from pathlib import Path
from settings import SettingsStore
from experiments import ExperimentStore

MAX_BYTES = 20 * 1024 * 1024


def export_personal(directory):
    directory = Path(directory)
    payload = {'version': 1, 'settings': SettingsStore(directory / 'settings.json').read(),
               'experiments': ExperimentStore(directory / 'experiments.json').read()}
    return json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def restore_personal(directory, content, confirmed=False):
    if confirmed is not True:
        raise ValueError('개인 설정·실험 기록 덮어쓰기를 확인해주세요.')
    if len(content) > MAX_BYTES:
        raise ValueError('백업 파일은 20MB 이하여야 합니다.')
    try:
        data = json.loads(content)
        if set(data) != {'version', 'settings', 'experiments'} or data['version'] != 1:
            raise ValueError('지원하지 않는 백업 형식입니다.')
        # Reuse the existing validators, including all filter/experiment rules.
        with tempfile.TemporaryDirectory() as temporary:
            validated = []
            for key, name, store in [('settings', 'settings.json', SettingsStore),
                                     ('experiments', 'experiments.json', ExperimentStore)]:
                path = Path(temporary) / name
                path.write_text(json.dumps(data[key], allow_nan=False), encoding='utf-8')
                validated.append((store, name, store(path).read()))
    except (ValueError, TypeError, KeyError, UnicodeError) as exc:
        raise ValueError(f'백업을 읽을 수 없습니다. 기존 파일을 보존했습니다: {exc}') from exc
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    originals = {name: (directory / name).read_bytes() if (directory / name).exists() else None
                 for _, name, _ in validated}
    try:
        for store, name, payload in validated:
            store(directory / name)._write(payload)
    except OSError:
        for name, original in originals.items():
            path = directory / name
            if original is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(original)
        raise
