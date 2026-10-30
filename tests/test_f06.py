from dataclasses import asdict
from datetime import timedelta
import json

import pytest

from backtesting import comparison_context, validate_round
from filters import build_context, f06, number_availability, DESCRIPTIONS
from settings import FilterSettings, SettingsStore, F06_MIN_OCCURRENCES
from validator import Draw, FIRST_DRAW


def history(occurrences):
    return [Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(),
                 (1 if r <= occurrences else 2, 20, 22, 24, 26, 28), 45)
            for r in range(1, 11)]


@pytest.mark.parametrize('count', [0, 1, 2, 3, 4, 10])
def test_f06_threshold_and_single_number_availability(count):
    ctx = build_context(history(count), ('F06',))
    assert (1 in ctx.f06_excluded) == (count >= 3)
    assert f06((1, 3, 5, 7, 9, 11), ctx.f06_excluded) == (count < 3)
    available, reasons = number_availability(ctx, ('F06',))
    assert (1 in available) == (count < 3)
    assert (1 in reasons) == (count >= 3)
    assert 45 not in ctx.f06_excluded  # Ten bonus appearances never count.


def test_f06_two_prior_occurrences_not_increased_by_target_or_future():
    prior = history(2)
    target = Draw(11, (FIRST_DRAW + timedelta(weeks=10)).isoformat(),
                  (1, 3, 5, 7, 9, 11), 45)
    rows = prior + [target]
    ctx = comparison_context(rows, 11, ('F06',))
    assert ctx.recent_rounds == tuple(range(1, 11))
    assert dict(ctx.frequencies)[1] == 2
    result = validate_round(rows, 11, ('F06',))
    verdict = next(v for v in result.filters if v.filter_id == 'F06')
    assert verdict.passed
    assert '3\ud68c' in verdict.reason
    assert validate_round(rows + [Draw(12, 'invalid future', (), 0)], 11, ('F06',)) == result


def test_f06_only_last_ten_confirmed_rounds_and_missing_round_rejected():
    rows = history(3)
    for r in range(11, 15):
        rows.append(Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(),
                         (2, 20, 22, 24, 26, 28), 45))
    ctx = comparison_context(rows, 14, ('F06',))
    assert ctx.recent_rounds == tuple(range(4, 14))
    assert 1 not in dict(ctx.frequencies)
    with pytest.raises(ValueError):
        comparison_context([d for d in rows if d.round != 8], 14, ('F06',))


def test_f06_existing_preset_schema_has_no_saved_threshold(tmp_path):
    # Existing presets persist the F06 enabled flag, never its old hardcoded 2.
    config = asdict(FilterSettings(enabled_ids=('F06',)))
    config.pop('f06_min_occurrences')
    assert not any('f06' in key.lower() for key in config)
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'version': 1, 'default': config,
                               'presets': {'existing': config}}), encoding='utf-8')
    before = path.read_bytes()
    store = SettingsStore(path)
    loaded = store.load_preset('existing')
    assert loaded == store.default()
    assert F06_MIN_OCCURRENCES == 3
    assert 1 not in build_context(history(2), loaded).f06_excluded
    assert 1 in build_context(history(3), loaded).f06_excluded
    assert path.read_bytes() == before
    assert '3\ud68c' in DESCRIPTIONS['F06']


def test_f04_three_f06_four_shared_by_extraction_backtest_and_saved_settings(tmp_path):
    from engine import analyze, generate_numbers
    from filters import f04

    config = FilterSettings(enabled_ids=('F04', 'F06'), section_max=3,
                            f06_min_occurrences=4)
    store = SettingsStore(tmp_path / 'selected.json')
    store.save_default(config)
    store.save_preset('selected', config)
    restored = SettingsStore(store.path)
    assert restored.default() == restored.load_preset('selected') == config
    assert FilterSettings.from_dict(json.loads(config.snapshot())) == config
    ctx = build_context(history(3), restored.default())
    assert 1 not in ctx.f06_excluded  # Three appearances remain allowed.
    assert 1 in build_context(history(4), config).f06_excluded
    result = analyze(ctx, config.combination_ids, seed=7,
                     universe=(1, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 30, 40))
    picks = generate_numbers(result, count=1)
    assert picks and all(f04(c, 3) and ctx.f06_excluded.isdisjoint(c) for c in picks)
    target = Draw(11, (FIRST_DRAW + timedelta(weeks=10)).isoformat(),
                  (1, 3, 5, 10, 30, 40), 45)
    allowed = validate_round(history(3) + [target], 11, config)
    assert all(v.passed for v in allowed.filters if v.filter_id in ('F04', 'F06'))
    rejected = validate_round(history(4) + [target], 11, config)
    assert not next(v for v in rejected.filters if v.filter_id == 'F06').passed
