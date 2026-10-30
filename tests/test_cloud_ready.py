import hashlib
import json
from pathlib import Path
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
import time
import pytest
from database import Database, seed_draws
from personal_backup import export_personal, restore_personal
from settings import FilterSettings, SettingsStore
from experiments import ExperimentStore, impact_analysis
from validator import Draw
from runtime_support import serialized_analysis
from scripts.build_cloud_bundle import build, FILES

ROOT = Path(__file__).resolve().parents[1]


def test_seed_complete_and_valid():
    draws = seed_draws()
    assert len(draws) == 1247
    assert draws[0].round == 1 and draws[-1].round == 1247


def test_seed_rejects_duplicates(tmp_path):
    data = json.loads((ROOT / 'seed/draws.json').read_text(encoding='utf-8'))
    data['draws'][1] = data['draws'][0]
    path = tmp_path / 'seed.json'
    path.write_text(json.dumps(data), encoding='utf-8')
    with pytest.raises(ValueError):
        seed_draws(path)


def test_isolated_default_bootstrap_and_no_replacement(tmp_path):
    import os
    env = {**os.environ, 'LOTTO_LAB_DATA_DIR': str(tmp_path)}
    code = ("from database import Database; d=Database(); "
            "assert len(d.all())==1247; d.set_meta('test','preserved')")
    subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env, check=True)
    path = tmp_path / 'lotto.db'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    subprocess.run([sys.executable, '-c',
                    "from database import Database; d=Database(); assert d.get_meta('test')=='preserved'"],
                   cwd=ROOT, env=env, check=True)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert Database(tmp_path / 'explicit.db').all() == []


def test_personal_backup_roundtrip_without_db(tmp_path):
    source, target = tmp_path / 'source', tmp_path / 'target'
    store = SettingsStore(source / 'settings.json')
    store.save_preset('legacy', FilterSettings.legacy())
    experiments = ExperimentStore(source / 'experiments.json')
    result = impact_analysis([Draw(1, '2002-12-07', (1, 2, 3, 4, 5, 6), 7)], 1, 1,
                             FilterSettings(enabled_ids=()))
    record = experiments.save('backup roundtrip', result)
    content = export_personal(source)
    restore_personal(target, content, confirmed=True)
    assert SettingsStore(target / 'settings.json').load_preset('legacy') == FilterSettings.legacy()
    assert export_personal(source) == export_personal(target)
    assert ExperimentStore(target / 'experiments.json').load(record['id']) == record
    assert not list(tmp_path.rglob('*.db'))


def test_restore_requires_confirmation_and_validates_before_writes(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    store.save_preset('preserved', FilterSettings())
    before = (tmp_path / 'settings.json').read_bytes()
    with pytest.raises(ValueError):
        restore_personal(tmp_path, export_personal(tmp_path))
    payload = json.loads(export_personal(tmp_path))
    payload['experiments'] = {'version': 999}
    with pytest.raises(ValueError):
        restore_personal(tmp_path, json.dumps(payload).encode(), confirmed=True)
    assert (tmp_path / 'settings.json').read_bytes() == before
    assert not (tmp_path / 'experiments.json').exists()


def test_bundle_allowlist_no_personal_files(tmp_path):
    output = build(tmp_path / 'cloud')
    relative = {p.relative_to(output).as_posix() for p in output.rglob('*') if p.is_file()}
    assert relative == set(FILES) | {'bundle-manifest.json'}
    assert not any(p.endswith(('.db', '.exe', 'secrets.toml')) for p in relative)
    assert not any('settings.json' in p or 'experiments.json' in p for p in relative)
    with pytest.raises(ValueError):
        build(output)


def test_expensive_job_gate_prevents_parallel_execution():
    running, maximum = 0, 0
    counter_lock = Lock()

    @serialized_analysis
    def job():
        nonlocal running, maximum
        with counter_lock:
            running += 1
            maximum = max(maximum, running)
        time.sleep(.02)
        with counter_lock:
            running -= 1
        return 1

    with ThreadPoolExecutor(max_workers=3) as executor:
        assert list(executor.map(lambda _: job(), range(3))) == [1, 1, 1]
    assert maximum == 1


def test_gate_released_after_failure():
    @serialized_analysis
    def fail():
        raise ValueError('expected')
    with pytest.raises(ValueError):
        fail()
    assert serialized_analysis(lambda: 7)() == 7
