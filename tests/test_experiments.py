from dataclasses import replace
from datetime import timedelta
import csv
import io
import json
import pytest
from experiments import (off_scenarios, impact_analysis, ab_analysis, precise_ab, analysis_csv,
                         ExperimentStore, experiment_csv, _outcomes)
from settings import FilterSettings, SettingsStore
from backtesting import backtest
from validator import Draw, FIRST_DRAW


def history():
    def draw(r, c):
        return Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(), c, 45)
    return [draw(r, (1, 3, 5, 7, 9, 11)) for r in range(1, 11)] + [
        draw(11, (2, 12, 20, 21, 30, 41)), draw(12, (2, 4, 6, 8, 10, 12)),
        draw(13, (12, 13, 20, 21, 30, 41)), draw(14, (2, 3, 4, 6, 8, 10))]


def test_off_scenarios_one_enabled_condition_at_a_time():
    cfg = FilterSettings(enabled_ids=('F03', 'F07', 'F13'), max_gap=17)
    scenarios = off_scenarios(cfg)
    assert [f for f, _ in scenarios] == ['F03', 'F07']
    for fid, changed in scenarios:
        assert set(cfg.enabled_ids) - set(changed.enabled_ids) == {fid}
        assert changed == replace(cfg, enabled_ids=tuple(f for f in cfg if f != fid))
        assert 'F13' in changed


def test_known_impact_baseline_delta_and_independent_rates():
    cfg = FilterSettings(enabled_ids=('F03', 'F07', 'F13'))
    result = impact_analysis(history(), 11, 14, cfg)
    assert result['baseline']['passed'] == 1 and result['baseline']['pass_rate'] == 0.25
    assert result['baseline']['evaluable'] == 4
    assert len(result['scenarios']) == 2 and 'F13' not in result['already_off']
    for scenario in result['scenarios']:
        assert scenario['passed'] == 2 and scenario['evaluable'] == 4
        assert scenario['delta_pp'] == scenario['common_delta_pp'] == 25
    independent = {s['filter']: s for s in result['independent_rates']}
    assert independent['F03']['pass_rate'] == independent['F07']['pass_rate'] == 0.5
    assert 'F13' not in independent


def test_impact_each_scenario_matches_existing_backtest():
    cfg = FilterSettings()
    result = impact_analysis(history(), 11, 14, cfg)
    assert result['baseline']['passed'] == backtest(history(), 11, 14, cfg).summary['passed']
    for s in result['scenarios']:
        reference = backtest(history(), 11, 14, FilterSettings.from_dict(s['settings'])).summary
        for key in ('passed', 'failed', 'evaluable', 'unavailable', 'pass_rate'):
            assert s[key] == reference[key]


def test_known_ab_intersection_and_differences():
    a, b = FilterSettings(enabled_ids=('F03',)), FilterSettings(enabled_ids=('F07',))
    result = ab_analysis(history(), 11, 14, a, b)
    assert result['a']['passed'] == result['b']['passed'] == 2
    assert result['round_groups']['both'] == [11]
    assert result['round_groups']['a_only'] == [12]
    assert result['round_groups']['b_only'] == [13]
    assert result['round_groups']['neither'] == [14]
    assert result['overlap']['common_evaluable'] == 4
    assert {d['field'] for d in result['differences']} == {'F03', 'F07'}
    changed = replace(b, max_gap=1)
    assert 'max_gap' in {d['field'] for d in ab_analysis(history(), 11, 14, a, changed)['differences']}


def test_f03_single_combo_and_f13_no_influence():
    a = FilterSettings(enabled_ids=('F03',))
    b = replace(a, enabled_ids=('F03', 'F13'), optimization_trials=1)
    result = ab_analysis(history(), 11, 14, a, b)
    assert result['a'] == result['b']
    assert result['a']['passed'] == 2  # 3:2 추출 비율을 회차 판정에 적용하지 않는다
    assert result['overlap']['both'] == 2
    assert all(d['role'] == '추출 전용·통과율 제외' for d in result['differences'])


def test_all_off_and_missing_target():
    rows = [r for r in history() if r.round != 7]
    cfg = FilterSettings(enabled_ids=())
    result = impact_analysis(rows, 1, 14, cfg)
    assert result['baseline']['passed'] == result['baseline']['evaluable'] == 13
    assert result['baseline']['unavailable'] == 1
    assert result['scenarios'] == []
    assert len(result['already_off']) == 12


def test_unavailable_separate_cohorts_not_counted_as_failures():
    a = FilterSettings(enabled_ids=())
    b = FilterSettings(enabled_ids=('F06',))
    result = ab_analysis(history(), 1, 11, a, b)
    assert result['a']['passed'] == 11 and result['a']['unavailable'] == 0
    assert result['b']['evaluable'] == result['b']['passed'] == 1
    assert result['b']['unavailable'] == 10 and result['b']['failed'] == 0
    assert result['overlap']['a_only'] == 0  # B 분석 불가를 B 실패로 간주하지 않음
    assert result['overlap']['common_evaluable'] == 1
    assert result['overlap']['a_pass_b_unavailable'] == 10
    impact = impact_analysis(history(), 1, 11, b)
    assert impact['scenarios'][0]['evaluable'] == 11
    assert impact['scenarios'][0]['common_evaluable'] == 1
    empty = impact_analysis(history(), 1, 2, b)
    assert empty['baseline']['pass_rate'] is None and empty['scenarios'][0]['delta_pp'] is None


def test_future_data_does_not_change_impact_or_ab():
    rows = history()
    future = rows + [Draw(15, 'bad future', (), 0)]
    a = FilterSettings(enabled_ids=('F11',))
    b = replace(a, history_scope='recent', history_n=2)
    for fn, args in ((impact_analysis, (a,)), (ab_analysis, (a, b))):
        before, after = fn(rows, 11, 14, *args), fn(future, 11, 14, *args)
        for key in before:
            if key not in ('analyzed_at', 'elapsed_seconds'):
                assert before[key] == after[key]


def test_one_raw_verdict_pass_reused_for_scenarios_and_flag_changes(monkeypatch):
    import experiments
    actual, calls = experiments.backtest, []

    def counted(*args):
        calls.append(args)
        return actual(*args)

    monkeypatch.setattr(experiments, 'backtest', counted)
    _outcomes.cache_clear()
    cfg = FilterSettings()
    impact_analysis(history(), 11, 14, cfg)
    ab_analysis(history(), 11, 14, cfg, replace(cfg, enabled_ids=()))
    assert len(calls) == 1


def test_precision_same_prior_basis_no_future_and_not_automatic(monkeypatch):
    import engine
    calls, actual = [], engine.analyze

    def small(ctx, enabled, seed):
        calls.append(ctx)
        return actual(ctx, enabled, seed, universe=range(1, 16))

    a = FilterSettings(enabled_ids=('F11',))
    b = replace(a, similarity_threshold=6)
    basic = ab_analysis(history(), 11, 14, a, b)
    assert calls == [] and 'candidates' not in json.dumps(basic)
    result = precise_ab(history() + [Draw(15, 'bad future', (), 0)], 14, a, b, small)
    assert len(calls) == 2
    assert all(c.basis_round == 13 and max(d.round for d in c.f11_history) == 13 for c in calls)
    assert result['a']['basis_round'] == result['b']['basis_round'] == 13
    with pytest.raises(ValueError):
        precise_ab(history(), 1, a, b, small)


def test_experiment_save_load_delete_and_preset_separation(tmp_path):
    preset = SettingsStore(tmp_path / 'settings.json')
    cfg = FilterSettings(enabled_ids=('F03',))
    preset.save_preset('설정', cfg)
    preset_before = preset.path.read_bytes()
    store = ExperimentStore(tmp_path / 'experiments.json')
    result = impact_analysis(history(), 11, 14, cfg)
    first = store.save('영향도', result)
    second = store.save('A/B', ab_analysis(history(), 11, 14, cfg, FilterSettings(enabled_ids=())))
    result['baseline']['passed'] = 999
    assert store.load(first['id'])['result']['baseline']['passed'] == 2
    assert len(store.read()['experiments']) == 2
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='확인'):
        store.delete(first['id'])
    assert before == store.path.read_bytes()
    store.delete(first['id'], confirmed=True)
    assert store.load(second['id'])['name'] == 'A/B'
    with pytest.raises(ValueError):
        store.load(first['id'])
    assert preset.path.read_bytes() == preset_before


def test_corrupt_experiments_preserved_and_invalid_name(tmp_path):
    store = ExperimentStore(tmp_path / 'experiments.json')
    result = impact_analysis(history(), 11, 14, FilterSettings())
    with pytest.raises(ValueError):
        store.save('', result)
    store.path.write_text('{bad', encoding='utf-8')
    before = store.path.read_bytes()
    with pytest.raises(ValueError, match='보존'):
        store.save('실험', result)
    assert before == store.path.read_bytes()


def test_csv_contains_period_snapshots_summary_and_actual_rounds(tmp_path):
    result = ab_analysis(history(), 11, 14, FilterSettings(enabled_ids=('F03',)), FilterSettings(enabled_ids=('F07',)))
    rows = list(csv.DictReader(io.StringIO(analysis_csv(result, True).decode('utf-8-sig'))))
    assert len(rows) == 4 and rows[0]['분석 시작'] == '11'
    assert json.loads(rows[0]['전체 설정'])['settings_a']['enabled_ids'] == ['F03']
    store = ExperimentStore(tmp_path / 'experiments.json')
    record = store.save('비교', result)
    exported = list(csv.DictReader(io.StringIO(experiment_csv(record).decode('utf-8-sig'))))
    assert len(exported) == 2 and exported[0]['실험 이름'] == '비교'


def test_experiment_precision_roundtrip_and_mismatched_settings_rejected(tmp_path):
    import engine
    cfg = FilterSettings(enabled_ids=('F03',))
    ab = ab_analysis(history(), 11, 14, cfg, cfg)
    calls = []

    def small(ctx, enabled, seed):
        calls.append(ctx)
        return engine.analyze(ctx, enabled, seed, universe=range(1, 16))

    precision = precise_ab(history(), 14, cfg, cfg, small)
    assert len(calls) == precision['computed_profiles'] == 1
    store = ExperimentStore(tmp_path / 'experiments.json')
    record = store.save('정밀 비교', ab, precision)
    assert store.load(record['id'])['precision']['basis_round'] == 13
    mismatched = json.loads(json.dumps(precision))
    mismatched['settings_a']['max_gap'] = 1
    with pytest.raises(ValueError, match='일치'):
        store.save('잘못된 정밀 비교', ab, mismatched)
    assert len(store.read()['experiments']) == 1


@pytest.mark.parametrize('start,end', [(0, 14), (14, 11), (11, 15)])
def test_invalid_ranges(start, end):
    with pytest.raises(ValueError):
        impact_analysis(history(), start, end, FilterSettings())
