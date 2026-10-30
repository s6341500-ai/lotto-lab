from collections import Counter
import csv
from dataclasses import replace
from datetime import timedelta
import io
from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from engine import (AnalysisResult, analyze, generate_numbers, coverage_stats, results_csv,
                    InsufficientCandidates)
from settings import FilterSettings
from filters import (build_context, classify_consecutive, f01, f02, f03, f04, f05, f06,
                     f07, f08, f09, f10, f11, f12, similarity_index)
from validator import Draw, FIRST_DRAW


def history():
    return [Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45)
            for r in range(1, 11)]


@pytest.fixture(scope='module')
def analyzed():
    results = {}
    for f03_enabled in (False, True):
        cfg = FilterSettings(enabled_ids=tuple(f for f in FilterSettings() if f != 'F03' or f03_enabled), pool_size=5)
        ctx = build_context(history(), cfg, 10)
        universe = (1, 2, 5, 7, 10, 12, 15, 18, 20, 21, 24, 27, 30, 31, 35, 40, 43, 45)
        result = analyze(ctx, cfg.combination_ids, seed=2026, universe=universe)
        assert dict(result.group_counts)['one_pair'] >= 6 and dict(result.group_counts)['none'] >= 4
        results[f03_enabled] = (ctx, result)
    return results


@pytest.mark.parametrize('count', (1, 5, 10))
@pytest.mark.parametrize('f03_enabled', (False, True))
@pytest.mark.parametrize('f13_enabled', (False, True))
def test_all_count_f03_f13_combinations(analyzed, count, f03_enabled, f13_enabled):
    ctx, result = analyzed[f03_enabled]
    selected = generate_numbers(result, optimize=f13_enabled, count=count)
    assert len(selected) == len(set(selected)) == count
    if f03_enabled and count > 1:
        assert Counter(classify_consecutive(c) for c in selected) == {'one_pair': 3 * count // 5, 'none': 2 * count // 5}
    sim = similarity_index(ctx.f11_history, ctx.settings.similarity_threshold)
    for c in selected:
        assert all([f01(c, ctx.f01_excluded), f02(c, ctx.previous), f04(c), f05(c), f06(c, ctx.f06_excluded),
                    f07(c), f08(c), f09(c), f10(c), f11(c, sim), f12(c)])
        if f03_enabled:
            assert f03(c)
    off = generate_numbers(result, optimize=False, count=count)
    if f13_enabled:
        assert coverage_stats(selected)['distinct_numbers'] >= coverage_stats(off)['distinct_numbers']
    assert coverage_stats(selected)['total_duplicates'] == 6 * count - len(set().union(*map(set, selected)))
    assert coverage_stats(selected)['distinct_numbers'] <= 45
    assert all(len(pool) <= 10 for _, pool in result.extended_group_samples)
    assert all(len(pool) <= 5 for _, pool in result.candidate_pools)


def test_single_draw_uses_actual_group_weights():
    one = (1, 2, 10, 20, 30, 40)
    none = tuple((1, 4, 10, 20, 30, n) for n in (34, 35, 36, 37, 38, 39, 40, 41, 42))
    result = AnalysisResult(10, ('F03',), 0, 10, (), (('one_pair', 1), ('none', 9), ('multiple', 0)),
                            (('one_pair', (one,)), ('none', none), ('multiple', ())), (), 0, '')
    counts = Counter(generate_numbers(replace(result, seed=s), count=1)[0] for s in range(5000))
    assert len(counts) == 10
    assert 400 <= counts[one] <= 600  # 1:1 그룹 추첨이면 약 2500번이 되어 실패한다.
    assert all(380 <= value <= 620 for value in counts.values())


def test_single_can_use_only_one_group_and_never_calls_f13(monkeypatch):
    import engine
    cfg = FilterSettings(enabled_ids=('F03', 'F13'))
    result = analyze(build_context(history(), cfg, 10), ('F03',), universe=(1, 3, 5, 7, 9, 11, 13))

    def forbid(*args):
        raise AssertionError('1조합은 F13 적용 대상이 아니다.')

    monkeypatch.setattr(engine, 'minimize_overlap', forbid)
    assert len(generate_numbers(result, count=1)) == 1
    with pytest.raises(InsufficientCandidates):
        generate_numbers(result, count=5)


def test_f03_off_has_no_group_quota_for_ten():
    cfg = FilterSettings(enabled_ids=())
    result = analyze(build_context(history(), cfg, 10), (), universe=range(1, 10))
    selected = generate_numbers(result, count=10, optimize=False)
    assert len(set(selected)) == 10
    assert all(classify_consecutive(c) == 'multiple' for c in selected)


@pytest.mark.parametrize('count', (1, 5, 10))
def test_csv_count_and_position_metadata(analyzed, count):
    result = analyzed[True][1]
    selected = generate_numbers(result, count=count)
    records = list(csv.DictReader(io.StringIO(results_csv(result, selected).decode('utf-8-sig'))))
    assert len(records) == count
    assert {r['추출 조합 수'] for r in records} == {str(count)}
    assert {r['총 번호 위치 수'] for r in records} == {str(6 * count)}
    if count == 1:
        assert records[0]['F13 결과 설명'] == '1조합: F13 적용 제외'


@pytest.mark.parametrize('count', (0, 2, 6, 11, True))
def test_invalid_counts(analyzed, count):
    with pytest.raises(ValueError):
        generate_numbers(analyzed[True][1], count=count)


def test_insufficient_total_and_group_candidates():
    cfg = FilterSettings(enabled_ids=())
    result = analyze(build_context(history(), cfg, 10), (), universe=range(1, 8))
    with pytest.raises(InsufficientCandidates, match='최소 10'):
        generate_numbers(result, count=10)
    result = replace(result, enabled=('F03',), group_counts=(('one_pair', 5), ('none', 20), ('multiple', 0)))
    with pytest.raises(InsufficientCandidates, match='최소 6'):
        generate_numbers(result, count=10)


def test_ui_count_switch_reuses_analysis_and_disables_f13(tmp_path, monkeypatch):
    import engine
    import database
    db = database.Database(tmp_path / 'counts.db')
    for r in history():
        db.save(r)
    monkeypatch.setattr(database, 'Database', lambda: db)
    actual, calls = engine.analyze, []

    def small(ctx, enabled, seed):
        calls.append(ctx)
        return actual(ctx, enabled, seed, universe=(1, 2, 5, 7, 10, 12, 15, 18, 20, 21, 24, 27, 30, 31, 35, 40, 43, 45))

    monkeypatch.setattr(engine, 'analyze', small)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('번호 추출').run()
    chooser = lambda: next(w for w in at.radio if w.label == '추출 조합 수')
    assert chooser().value == 5
    next(b for b in at.button if b.label == '전체 조합 분석 및 5조합 추출').click().run(timeout=20)
    assert not at.exception and len(calls) == 1
    chooser().set_value(1).run()
    assert not at.exception and len(calls) == 1
    assert at.checkbox[12].disabled
    assert any(t.value == '최종 1조합' for t in at.subheader)
    chooser().set_value(10).run()
    assert not at.exception and len(calls) == 1
    assert not at.checkbox[12].disabled
    assert any(t.value == '최종 10조합' for t in at.subheader)
    assert any('60개' in c.value for c in at.caption)
    chooser().set_value(5).run()
    assert any(t.value == '최종 5조합' for t in at.subheader)
