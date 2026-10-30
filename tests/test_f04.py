from dataclasses import asdict
from itertools import combinations
import json
from math import comb
from pathlib import Path

import pytest

from backtesting import validate_round
from engine import analyze, generate_numbers
from filters import build_context, f04
from settings import FilterSettings, SettingsStore
from validator import Draw


@pytest.mark.parametrize('maximum', [2, 3, 4, 5, 6])
def test_f04_selected_limit_reaches_analysis_and_extraction(maximum):
    settings = FilterSettings(enabled_ids=('F04',), section_max=maximum)
    ctx = build_context([Draw(1, '2002-12-07', (1, 2, 3, 10, 20, 30), 45)], settings)
    universe = tuple(range(1, 7)) + tuple(range(10, 14)) + tuple(range(20, 24))
    result = analyze(ctx, settings.combination_ids, seed=7, universe=universe)
    expected = sum(f04(c, maximum) for c in combinations(universe, 6))
    assert result.remaining == expected
    assert all(f04(c, maximum) for c in generate_numbers(result, count=1))
    assert f04((1, 2, 3, 4, 5, 6), maximum) == (maximum == 6)
    if maximum == 6:
        assert result.remaining == comb(len(universe), 6)


def test_f04_existing_bucket_boundaries_and_default():
    assert f04((1, 9, 10, 19, 40, 45))
    assert not f04((10, 11, 19, 20, 30, 40))
    assert f04((10, 11, 19, 20, 30, 40), 3)
    assert FilterSettings().section_max == 2


@pytest.mark.parametrize('maximum', [2, 3, 4, 5, 6])
def test_f04_settings_roundtrip_and_backtest(maximum, tmp_path):
    config = FilterSettings(enabled_ids=('F04',), section_max=maximum)
    store = SettingsStore(tmp_path / 'settings.json')
    store.save_default(config)
    store.save_preset('selected', config)
    restored = SettingsStore(store.path)
    assert restored.default() == config
    assert restored.load_preset('selected') == config
    assert FilterSettings.from_dict(json.loads(config.snapshot())) == config
    result = validate_round([Draw(1, '2002-12-07', (1, 2, 3, 10, 20, 30), 45)], 1, config)
    verdict = next(v for v in result.filters if v.filter_id == 'F04')
    assert verdict.passed == (maximum >= 3)


def test_f04_old_preset_defaults_without_modifying_file(tmp_path):
    data = asdict(FilterSettings())
    data.pop('section_max')
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'version': 1, 'default': data,
                               'presets': {'existing': data}}), encoding='utf-8')
    before = path.read_bytes()
    assert SettingsStore(path).default().section_max == 2
    assert SettingsStore(path).load_preset('existing').section_max == 2
    assert path.read_bytes() == before


@pytest.mark.parametrize('invalid', [1, 7, True, 2.5])
def test_f04_invalid_setting_rejected(invalid):
    with pytest.raises(ValueError):
        FilterSettings(section_max=invalid)


def test_f04_dropdown_applies_and_survives_menu_change(tmp_path, monkeypatch):
    import database
    from streamlit.testing.v1 import AppTest

    db = database.Database(tmp_path / 'ui.db')
    db.save(Draw(1, '2002-12-07', (1, 2, 3, 10, 20, 30), 45))
    monkeypatch.setattr(database, 'Database', lambda: db)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    app.session_state['startup_checked'] = True
    app.run()
    app.sidebar.radio[0].set_value('조건 설정').run()
    selector = next(s for s in app.selectbox if s.label.startswith('F04 '))
    assert selector.value == 2 and len(selector.options) == 5
    selector.set_value(4).run()
    assert not app.exception
    assert app.session_state['numeric_config']['section_max'] == 4
    app.sidebar.radio[0].set_value('번호 추출').run()
    assert next(s for s in app.selectbox if s.label.startswith('F04 ')).value == 4
    assert not app.exception
