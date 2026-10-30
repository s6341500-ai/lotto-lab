from collections import Counter
from dataclasses import replace, asdict
from datetime import timedelta
from itertools import combinations
import json
import random
import pytest
from settings import FilterSettings, SettingsStore, COMBINATION_FILTER_IDS
from filters import (f08, f09, f10, f11, f12, similarity_index, select_similarity_history,
                     build_context, classify_consecutive)
from engine import analyze, generate_numbers, coverage_stats, results_csv, stages_csv
from backtesting import validate_round, backtest, comparison_context
from validator import Draw, FIRST_DRAW


def draw(r, combo=(1, 3, 5, 7, 9, 11), bonus=45):
    return Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(), combo, bonus)


@pytest.mark.parametrize('combo,maximum,expected', [
    ((1, 11, 22, 23, 34, 45), 2, True),
    ((1, 11, 21, 23, 34, 45), 2, False),
    ((1, 11, 21, 23, 34, 45), 3, True),
    ((1, 12, 23, 34, 40, 45), 1, True),
    ((10, 20, 30, 31, 42, 43), 2, False),
])
def test_f08_boundary(combo, maximum, expected):
    assert f08(combo, maximum) is expected


@pytest.mark.parametrize('combo,maximum,expected', [
    ((1, 2, 3, 23, 24, 25), 20, True),
    ((1, 2, 3, 24, 25, 26), 20, False),
    ((1, 2, 3, 24, 25, 26), 21, True),
    ((21, 22, 23, 24, 25, 26), 1, True),  # 바깥쪽 1/45까지의 간격은 제외
])
def test_f09_boundary(combo, maximum, expected):
    assert f09(combo, maximum) is expected


@pytest.mark.parametrize('combo,expected', [
    ((1, 22, 23, 24, 30, 45), True), ((1, 2, 3, 22, 23, 45), True),
    ((1, 23, 24, 25, 30, 45), False), ((1, 2, 3, 4, 22, 23), False),
])
def test_f10_boundary(combo, expected):
    assert f10(combo) is expected
    assert f10((1, 2, 3, 4, 5, 6), 3, 3, 3, 3, 3)


@pytest.mark.parametrize('threshold', range(1, 7))
def test_f11_index_matches_naive_intersection(threshold):
    rng = random.Random(77)
    history = tuple(draw(r, tuple(sorted(rng.sample(range(1, 46), 6)))) for r in range(1, 21))
    index = similarity_index(history, threshold)
    masks = similarity_index(history, threshold, 'masks')
    candidates = [d.numbers for d in history] + [tuple(sorted(rng.sample(range(1, 46), 6))) for _ in range(50)]
    for c in candidates:
        expected = not any(len(set(c) & set(old.numbers)) >= threshold for old in history)
        assert f11(c, index) == f11(c, masks) == expected


def test_f11_five_six_and_bonus_exclusion():
    index = similarity_index((draw(1, (1, 2, 3, 4, 5, 6), 45),), 5)
    assert not f11((1, 2, 3, 4, 5, 7), index)
    assert not f11((1, 2, 3, 4, 5, 6), index)
    assert f11((1, 2, 3, 4, 7, 45), index)


@pytest.mark.parametrize('combo,expected', [
    ((1, 2, 3, 4, 45, 44), False),  # 99
    ((1, 2, 3, 5, 44, 45), True),   # 100
    ((10, 20, 30, 35, 38, 42), True),  # 175
    ((10, 20, 30, 35, 39, 42), False),  # 176
])
def test_f12_inclusive_boundaries(combo, expected):
    assert f12(combo) is expected
    assert f12(combo, sum(combo), sum(combo))


@pytest.mark.parametrize('changes', [
    {'end_digit_max': 0}, {'max_gap': 45}, {'low_boundary': 0}, {'low_min': 5, 'low_max': 4},
    {'low_min': 4, 'high_min': 4}, {'sum_min': 176, 'sum_max': 175},
    {'similarity_threshold': 7}, {'history_scope': 'future'}, {'history_n': 0},
    {'pool_size': 999999}, {'optimization_trials': 0}, {'max_gap': True},
    {'enabled_ids': ('F99',)},
])
def test_invalid_settings(changes):
    with pytest.raises(ValueError):
        FilterSettings(**changes)


def test_presets_defaults_restore_confirmation_and_db_separation(tmp_path):
    store = SettingsStore(tmp_path / 'settings.json')
    original = FilterSettings()
    assert store.default() == original and not store.path.exists()
    custom = replace(original, max_gap=12, history_scope='recent', history_n=30)
    store.save_default(custom)
    store.save_preset('내 설정', custom)
    store.save_preset('기존 결과', FilterSettings.legacy())
    assert SettingsStore(store.path).default() == custom
    assert store.load_preset('내 설정') == custom
    assert store.load_preset('기존 결과') == FilterSettings.legacy()
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='확인'):
        store.delete_preset('내 설정')
    assert store.path.read_bytes() == before
    store.delete_preset('내 설정', confirmed=True)
    with pytest.raises(ValueError):
        store.load_preset('내 설정')
    assert store.default() == custom  # 프리셋 삭제가 기본값을 바꾸지 않는다


def test_corrupt_settings_preserved(tmp_path):
    path = tmp_path / 'bad.json'
    path.write_text('{broken', encoding='utf-8')
    before = path.read_bytes()
    store = SettingsStore(path)
    with pytest.raises(ValueError, match='보존'):
        store.save_default(FilterSettings())
    assert path.read_bytes() == before


def test_history_scopes_strict_basis_and_backtest_no_leak():
    rows = [draw(r, (2, 12, 20, 21, 30, 41) if r == 1 else (1, 3, 5, 7, 9, 11)) for r in range(1, 11)]
    rows.append(draw(11, (2, 12, 20, 21, 30, 41)))
    cfg = FilterSettings(enabled_ids=('F11',))
    result = validate_round(rows, 11, cfg)
    assert result.overall_passed is False  # 10회 창보다 오래된 이력도 참조
    recent = replace(cfg, history_scope='recent', history_n=2)
    assert validate_round(rows, 11, recent).overall_passed is True
    assert validate_round(rows, 11, recent) == validate_round(rows + [Draw(12, 'bad', (), 0)], 11, recent)
    assert validate_round(rows, 11, cfg) == validate_round(rows + [Draw(12, 'bad', (), 0)], 11, cfg)
    changed = rows[:-1] + [draw(11, (4, 6, 8, 10, 12, 14))]
    assert comparison_context(rows, 11, cfg) == comparison_context(changed, 11, cfg)
    # 추출 분석은 명시된 기준 회차 미포함, 실제 검증/비교는 검증 대상 미포함.
    assert max(d.round for d in build_context(rows, cfg, 11).f11_history) == 10
    assert max(d.round for d in comparison_context(rows, 11, cfg).f11_history) == 10
    target12 = rows + [draw(12, (2, 12, 20, 21, 30, 41))]
    assert max(d.round for d in comparison_context(target12, 12, cfg).f11_history) == 11


def test_f11_old_history_outside_ten_rounds_and_missing_history():
    rows = [draw(r) for r in range(1, 21)]
    rows[0] = draw(1, (2, 12, 20, 21, 30, 41))
    rows.append(draw(21, (2, 12, 20, 21, 30, 41)))
    cfg = FilterSettings(enabled_ids=('F11',))
    assert validate_round(rows, 21, cfg).overall_passed is False
    assert validate_round(rows[1:], 21, cfg).overall_passed is None
    cfg = replace(cfg, history_scope='recent', history_n=5)
    assert validate_round(rows[1:], 21, cfg).overall_passed is True


def test_new_backtest_flags_and_f13_not_a_verdict():
    rows = [draw(r) for r in range(1, 11)] + [draw(11, (2, 12, 20, 21, 30, 41))]
    result = backtest(rows, 11, 11, FilterSettings())
    assert len(result.rows[0].filters) == len(result.summary['filters']) == 12
    assert result.summary['passed'] == 1
    cfg_off = replace(FilterSettings(), enabled_ids=tuple(f for f in FilterSettings() if f != 'F13'))
    assert backtest(rows, 11, 11, cfg_off).summary == result.summary
    assert all(v.filter_id != 'F13' for v in result.rows[0].filters)
    narrow = replace(FilterSettings(), max_gap=1)
    assert backtest(rows, 11, 11, narrow).summary['passed'] == 0


@pytest.mark.parametrize('fid', COMBINATION_FILTER_IDS[7:])
def test_new_filter_on_off_and_stage_accounting(fid):
    rows = [draw(r) for r in range(1, 11)]
    cfg = FilterSettings(enabled_ids=(fid,))
    ctx = build_context(rows, cfg, 10)
    result = analyze(ctx, cfg.combination_ids, universe=range(1, 16))
    assert len(result.stages) == 12
    assert result.total == result.remaining + sum(s.removed for s in result.stages)
    assert all(s.removed == 0 for s in result.stages if s.filter_id != fid)
    off = FilterSettings(enabled_ids=())
    result_off = analyze(build_context(rows, off, 10), (), universe=range(1, 16))
    assert result_off.remaining == result_off.total


def test_f13_improves_never_worsens_and_keeps_quota_and_validity():
    rows = [draw(r) for r in range(1, 11)]
    cfg = FilterSettings(enabled_ids=('F03', 'F08', 'F09', 'F10', 'F12', 'F13'), pool_size=128)
    ctx = build_context(rows, cfg, 10)
    universe = (1, 2, 5, 7, 10, 12, 15, 18, 20, 21, 24, 27, 30, 31, 35, 40, 43, 45)
    result = analyze(ctx, cfg.combination_ids, seed=19, universe=universe)
    off, on = generate_numbers(result, optimize=False), generate_numbers(result)
    assert len(on) == len(set(on)) == 5
    assert Counter(classify_consecutive(c) for c in on) == {'one_pair': 3, 'none': 2}
    assert coverage_stats(on)['distinct_numbers'] >= coverage_stats(off)['distinct_numbers']
    assert coverage_stats(on)['distinct_numbers'] <= len(universe)
    assert coverage_stats(on)['total_duplicates'] == 30 - len(set().union(*map(set, on)))
    for c in on:
        assert f08(c) and f09(c) and f10(c) and f12(c)
    assert all(len(pool) <= cfg.pool_size for _, pool in result.candidate_pools)
    assert on == generate_numbers(result)
    assert '설정 스냅샷' in results_csv(result, on).decode('utf-8-sig')
    assert '중복 최소화 시도 결과' in results_csv(result, on).decode('utf-8-sig')
    assert '분석 기준 회차' in stages_csv(result).decode('utf-8-sig')


def test_f13_off_preserves_legacy_random_sample():
    rows = [draw(r) for r in range(1, 11)]
    old = analyze(build_context(rows, ('F03',), 10), ('F03',), 2026, universe=range(1, 16))
    cfg = FilterSettings(enabled_ids=('F03',))
    new = analyze(build_context(rows, cfg, 10), ('F03',), 2026, universe=range(1, 16))
    assert generate_numbers(old) == generate_numbers(new)


def test_f13_can_reach_thirty_when_available_in_pool():
    from engine import AnalysisResult
    ones = ((1, 2, 4, 6, 8, 10), (12, 13, 15, 17, 19, 21), (23, 24, 26, 28, 30, 32))
    nones = ((3, 7, 11, 16, 20, 25), (5, 9, 14, 18, 22, 27))
    decoy_ones = ((1, 2, 4, 6, 8, 12), (1, 2, 4, 6, 8, 14), (1, 2, 4, 6, 8, 16))
    decoy_nones = ((1, 4, 8, 12, 16, 20), (1, 4, 8, 12, 16, 22))
    cfg = FilterSettings(enabled_ids=('F03', 'F13'))
    result = AnalysisResult(10, ('F03',), 2026, 10, (), (('none', 4), ('one_pair', 6), ('multiple', 0)),
                            (('one_pair', decoy_ones), ('none', decoy_nones), ('multiple', ())), (), 0.0, '', cfg,
                            (('one_pair', ones + decoy_ones), ('none', nones + decoy_nones), ('multiple', ())))
    on = generate_numbers(result)
    assert coverage_stats(on)['distinct_numbers'] == 30
    assert len(set(on)) == 5
    assert Counter(classify_consecutive(c) for c in on) == {'one_pair': 3, 'none': 2}


def test_f13_off_ignores_optimizer_pool():
    rows = [draw(r) for r in range(1, 11)]
    cfg = FilterSettings(enabled_ids=('F03', 'F13'))
    r = analyze(build_context(rows, cfg, 10), cfg.combination_ids, universe=range(1, 16))
    assert generate_numbers(r, optimize=False) == generate_numbers(replace(r, candidate_pools=()), optimize=False)


def test_legacy_subset_csv_snapshot_matches_actual_enabled_filters():
    import csv
    import io
    rows = [draw(r) for r in range(1, 11)]
    result = analyze(build_context(rows, ('F03',), 10), ('F03',), universe=range(1, 16))
    picks = generate_numbers(result)
    exported = list(csv.DictReader(io.StringIO(results_csv(result, picks).decode('utf-8-sig'))))
    assert json.loads(exported[0]['설정 스냅샷'])['enabled_ids'] == ['F03']
