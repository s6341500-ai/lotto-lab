from collections import Counter
from dataclasses import replace
from datetime import timedelta
from itertools import combinations
from math import comb
import csv
import io
import pytest
from filters import (FILTER_IDS, FilterContext, build_context, number_availability,
                     f01, f02, f03, f04, f05, f06, f07, consecutive_pairs, classify_consecutive)
from engine import analyze, generate_numbers, InsufficientCandidates, TOTAL_COMBINATIONS, results_csv
from validator import Draw, FIRST_DRAW


def history(count=10):
    return [Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45)
            for i in range(1, count + 1)]


def context():
    return build_context(history())


@pytest.mark.parametrize('fn,args,allowed,rejected', [
    (f01, (frozenset({1, 2}),), (3, 5, 7, 9, 11, 13), (1, 5, 7, 9, 11, 13)),
    (f02, (frozenset({1, 2, 3, 4, 5, 6}),), (1, 8, 10, 12, 14, 16), (1, 2, 10, 12, 14, 16)),
    (f03, (), (1, 2, 10, 20, 30, 40), (1, 2, 10, 11, 30, 40)),
    (f04, (), (1, 9, 10, 19, 40, 45), (10, 11, 19, 20, 30, 40)),
    (f05, (), (1, 2, 10, 11, 30, 40), (1, 2, 3, 20, 30, 40)),
    (f06, (frozenset({1, 2}),), (3, 5, 7, 9, 11, 13), (1, 5, 7, 9, 11, 13)),
    (f07, (), (1, 2, 3, 4, 5, 6), (1, 3, 5, 7, 9, 10)),
])
def test_each_filter(fn, args, allowed, rejected):
    assert fn(allowed, *args)
    assert not fn(rejected, *args)


@pytest.mark.parametrize('odds,allowed', [(0, False), (1, False), (2, True), (3, True), (4, True), (5, False), (6, False)])
def test_f07_ratios(odds, allowed):
    c = tuple(sorted(tuple(range(1, 12, 2))[:odds] + tuple(range(2, 13, 2))[:6 - odds]))
    assert f07(c) == allowed


@pytest.mark.parametrize('combo,count,group', [
    ((1, 3, 5, 7, 9, 11), 0, 'none'),
    ((1, 2, 5, 7, 9, 11), 1, 'one_pair'),
    ((1, 2, 3, 7, 9, 11), 2, 'multiple'),
    ((1, 2, 5, 6, 9, 11), 2, 'multiple'),
    ((1, 2, 3, 4, 9, 11), 3, 'multiple'),
    ((1, 2, 3, 4, 5, 6), 5, 'multiple'),
])
def test_consecutive_count(combo, count, group):
    assert consecutive_pairs(combo) == count
    assert classify_consecutive(combo) == group


def test_f01_f06_overlap_and_no_bonus():
    ctx = context()
    assert ctx.f01_excluded <= ctx.f06_excluded
    assert 45 not in ctx.f06_excluded  # 모든 회차의 보너스지만 제외되지 않음
    available, reasons = number_availability(ctx)
    assert 45 in available and 1 not in available
    assert len(reasons[1]) == 2
    assert number_availability(ctx, ())[0] == tuple(range(1, 46))
    # F01을 먼저 적용하면 F06의 제거 수에 중복 집계되지 않는다.
    result = analyze(ctx, ('F01', 'F06'), universe=range(1, 15))
    assert result.stages[0].removed > 0
    assert result.stages[5].removed == 0


def test_exactly_two_occurrences_threshold():
    rows = history()
    rows[-1] = replace(rows[-1], numbers=(2, 4, 6, 8, 10, 12))
    rows[-2] = replace(rows[-2], numbers=(2, 14, 16, 18, 20, 22))
    ctx = build_context(rows)
    assert 2 in ctx.f06_excluded and 4 not in ctx.f06_excluded
    assert ctx.f01_excluded == {2}


def test_data_shortage_and_gaps():
    with pytest.raises(ValueError, match='데이터'):
        build_context([])
    with pytest.raises(ValueError, match='부족'):
        build_context(history(9))
    with pytest.raises(ValueError, match='부족'):
        build_context(history(10)[1:])
    with pytest.raises(ValueError, match='부족'):
        build_context(history(1), ('F01',))
    assert build_context(history(1), ('F02',)).basis_round == 1
    with pytest.raises(ValueError, match='부족'):
        analyze(build_context(history(1), ()), ('F06',), universe=range(1, 8))


def test_basis_no_future_leak():
    rows = history(11)
    assert build_context(rows, basis_round=10) == build_context(rows[:10])


def test_sampling_ratio_unique_reproducible():
    result = analyze(context(), ('F03',), seed=50, universe=range(1, 16))
    picks = generate_numbers(result)
    assert len(picks) == len(set(picks)) == 5
    assert Counter(classify_consecutive(c) for c in picks) == {'one_pair': 3, 'none': 2}
    assert result == replace(analyze(context(), ('F03',), seed=50, universe=range(1, 16)),
                             elapsed_seconds=result.elapsed_seconds, analyzed_at=result.analyzed_at)
    assert all(len(sample) <= 5 for _, sample in result.group_samples)
    assert len(result.overall_sample) <= 5
    assert generate_numbers(result) == picks
    rows = list(csv.reader(io.StringIO(results_csv(result, picks, 'collection-time').decode('utf-8-sig'))))
    assert len(rows) == 6 and rows[1][1] == 'collection-time'


def test_f03_off_and_independent_f05():
    result = analyze(context(), (), universe=range(1, 10))
    assert result.remaining == comb(9, 6)
    assert dict(result.group_counts)['multiple'] > 0
    assert len(set(generate_numbers(result))) == 5
    r5 = analyze(context(), ('F05',), universe=range(1, 10))
    assert all(f05(c) for c in generate_numbers(r5))
    assert r5.stages[4].removed > 0
    r35 = analyze(context(), ('F03', 'F05'), universe=range(1, 15))
    assert r35.stages[4].removed == 0


def test_candidate_shortage():
    with pytest.raises(InsufficientCandidates):
        generate_numbers(analyze(context(), ('F03',), universe=range(1, 8)))
    with pytest.raises(InsufficientCandidates):
        generate_numbers(analyze(context(), (), universe=range(1, 7)))
    with pytest.raises(InsufficientCandidates):
        generate_numbers(analyze(context(), ('F03',), universe=(1, 3, 5, 7, 9, 11, 13, 15)))


def test_total_combinations_by_actual_iteration():
    assert TOTAL_COMBINATIONS == 8_145_060
    assert sum(1 for _ in combinations(range(1, 46), 6)) == 8_145_060


def test_counts_against_independent_materialized_small_universe():
    ctx = context()
    universe = range(1, 17)
    checks = [lambda c: f01(c, ctx.f01_excluded), lambda c: f02(c, ctx.previous),
              f03, f04, f05, lambda c: f06(c, ctx.f06_excluded), f07]
    for enabled in [FILTER_IDS, ('F02', 'F04', 'F07'), (), ('F03', 'F05')]:
        result = analyze(ctx, enabled, universe=universe)
        candidates = list(combinations(universe, 6))
        for index, fid in enumerate(FILTER_IDS):
            before = len(candidates)
            if fid in enabled:
                candidates = [c for c in candidates if checks[index](c)]
            assert (result.stages[index].entered, result.stages[index].removed, result.stages[index].passed) == (before, before - len(candidates), len(candidates))
        assert result.remaining == len(candidates)
        assert dict(result.group_counts) == {g: sum(classify_consecutive(c) == g for c in candidates) for g in ('none', 'one_pair', 'multiple')}


@pytest.mark.parametrize('fid', FILTER_IDS)
def test_individual_switch_and_fixed_order(fid):
    result = analyze(context(), (fid,), universe=range(1, 15))
    assert tuple(s.filter_id for s in result.stages) == FILTER_IDS
    for stage in result.stages:
        assert stage.enabled == (stage.filter_id == fid)
        assert stage.entered == stage.passed + stage.removed
        if not stage.enabled:
            assert stage.removed == 0
    assert result.total == result.remaining + sum(s.removed for s in result.stages)
