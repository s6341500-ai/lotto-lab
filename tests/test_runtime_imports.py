"""AppTest 외에 실제 Python 프로세스의 import 경로와 export를 검증한다."""
import ast
import importlib
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_local_from_import_names_exist():
    for source in ROOT.glob('*.py'):
        for node in ast.walk(ast.parse(source.read_text(encoding='utf-8'))):
            if isinstance(node, ast.ImportFrom) and node.module and (ROOT / f'{node.module}.py').exists():
                module = importlib.import_module(node.module)
                assert Path(module.__file__).resolve() == (ROOT / f'{node.module}.py').resolve()
                for name in node.names:
                    assert hasattr(module, name.name), (source.name, node.module, name.name)


@pytest.mark.parametrize('missing_export', [None, 'coverage_stats', 'stages_csv'])
def test_app_import_in_fresh_python_and_stale_engine_recovery(tmp_path, missing_export):
    script = r'''
import hashlib
import logging
from pathlib import Path
import sys
logging.disable(logging.CRITICAL)
import database
import collector
import engine
db = database.Database(sys.argv[1])
before = hashlib.sha256(db.path.read_bytes()).hexdigest()
database.Database = lambda: db
def forbid_collection(*args, **kwargs):
    raise AssertionError('Bare import must not collect or modify data')
collector.Collector.update = forbid_collection
missing = sys.argv[2]
if missing != 'none':
    delattr(engine, missing)
    try:
        exec('from engine import ' + missing)
    except ImportError:
        pass
    else:
        raise AssertionError('Stale-module ImportError was not reproduced')
import app
for name in ('analyze', 'generate_numbers', 'results_csv', 'stages_csv', 'InsufficientCandidates', 'coverage_stats'):
    assert getattr(app, name) is getattr(engine, name)
assert Path(engine.__file__).resolve() == Path(app.__file__).resolve().with_name('engine.py')
assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
assert engine.coverage_stats(((1, 2, 3, 4, 5, 6),))['distinct_numbers'] == 6
print('APP_IMPORT_OK')
'''
    result = subprocess.run([sys.executable, '-c', script, str(tmp_path / 'import.db'), missing_export or 'none'],
                            cwd=ROOT, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'APP_IMPORT_OK' in result.stdout
