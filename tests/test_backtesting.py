import csv
import hashlib
import io
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import pytest
from backtesting import (validate_round, backtest, comparison_context, _validate_snapshot,
                         read_snapshot, rounds_csv, summary_csv)
from validator import Draw, FIRST_DRAW
from filters import FILTER_IDS


def draw(r, numbers=(1, 3, 5, 7, 9, 11), bonus=45):
    return Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(), numbers, bonus)


def history(combo=(2, 12, 20, 21, 30, 41)):
    return [draw(r) for r in range(1, 11)] + [draw(11, combo)]


def verdict(result, fid):
    return next(v for v in result.filters if v.filter_id == fid)


def test_target_and_future_are_excluded_from_context():
    rows = history()
    changed = rows + [Draw(12, 'invalid future date', (999,), -1)]
    assert validate_round(rows, 11) == validate_round(changed, 11)
    first_context = comparison_context(rows, 11)
    rows[-1] = draw(11, (4, 6, 8, 10, 12, 14))
    assert comparison_context(rows, 11) == first_context
    assert first_context.basis_round == 10
    assert first_context.recent_rounds == tuple(range(1, 11))


def test_current_draw_does_not_increase_f06_frequency():
    rows = history()
    rows[0] = draw(1, (2, 3, 5, 7, 9, 11))  # 2는 과거에 단 한 번
    result = validate_round(rows, 11)
    assert verdict(result, 'F06').passed is True
    assert verdict(result, 'F02').passed is True  # 대상 자체를 직전 회차로 쓰면 거부될 사례
    assert 2 not in dict(result.excluded_numbers)


def test_future_mutations_do_not_change_backtest_rows_or_summary():
    rows = history() + [draw(12, (4, 12, 20, 24, 30, 41))]
    before = backtest(rows, 11, 12)
    after = backtest(rows + [Draw(13, 'bad', (), 0)], 11, 12)
    assert before.rows == after.rows
    assert before.summary == after.summary


@pytest.mark.parametrize('fid,combo,passed', [
    ('F01', (2, 12, 20, 21, 30, 41), True),
    ('F01', (1, 12, 20, 21, 30, 41), False),
    ('F02', (1, 12, 20, 21, 30, 41), True),
    ('F02', (1, 3, 12, 20, 30, 41), False),
    ('F03', (2, 12, 20, 21, 30, 41), True),
    ('F03', (12, 13, 20, 21, 30, 41), False),
    ('F04', (2, 12, 20, 21, 30, 41), True),
    ('F04', (12, 14, 16, 20, 30, 41), False),
    ('F05', (12, 13, 20, 21, 30, 41), True),
    ('F05', (12, 13, 14, 20, 30, 41), False),
    ('F06', (2, 12, 20, 21, 30, 41), True),
    ('F06', (1, 12, 20, 21, 30, 41), False),
    ('F07', (2, 12, 20, 21, 30, 41), True),
    ('F07', (2, 12, 20, 24, 30, 40), False),
])
def test_each_real_draw_verdict(fid, combo, passed):
    result = validate_round(history(combo), 11)
    v = verdict(result, fid)
    assert v.passed is passed
    assert v.reason


@pytest.mark.parametrize('combo,pattern,count', [
    ((2, 12, 20, 24, 30, 41), 'none', 0),
    ((2, 12, 20, 21, 30, 41), 'one_pair', 1),
    ((12, 13, 20, 21, 30, 41), 'multiple', 2),
    ((12, 13, 14, 20, 30, 41), 'multiple', 2),
])
def test_f03_real_draw_classification(combo, pattern, count):
    result = validate_round(history(combo), 11)
    assert result.consecutive_pattern == pattern
    assert result.pair_count == count
    assert verdict(result, 'F03').passed == (count <= 1)
    assert '3:2' in verdict(result, 'F03').reason


def test_on_off_and_raw_independent_verdicts():
    rows = history((1, 12, 20, 21, 30, 41))
    assert validate_round(rows, 11).failed_filters == ('F01', 'F06')
    result = validate_round(rows, 11, tuple(f for f in FILTER_IDS if f not in ('F01', 'F06')))
    assert result.overall_passed is True
    assert verdict(result, 'F01').passed is False and not verdict(result, 'F01').enabled
    assert result.excluded_numbers == ()
    assert validate_round(rows, 11, ()).overall_passed is True


def test_insufficient_history_denominators_and_all_off():
    rows = history()
    result = backtest(rows, 1, 11)
    s = result.summary
    assert s['requested'] == 11 and s['evaluable'] == 1 and s['unavailable'] == 10
    assert s['passed'] == 1 and s['pass_rate'] == 1
    denominators = {f['filter']: f['evaluable'] for f in s['filters']}
    assert denominators == dict(F01=9, F02=10, F03=11, F04=11, F05=11, F06=1, F07=11)
    assert validate_round(rows, 1).overall_passed is None
    assert validate_round(rows, 1, ()).overall_passed is True
    assert validate_round(rows, 1, ('F03',)).overall_passed is True


def test_gap_and_missing_target_are_not_failures():
    rows = [r for r in history() if r.round != 2]
    assert validate_round(rows, 11).overall_passed is None
    assert verdict(validate_round(rows, 11), 'F02').passed is True
    missing = validate_round(rows, 2, ())
    assert missing.overall_passed is None and missing.numbers == ()
    s = backtest(rows, 1, 11).summary
    assert s['evaluable'] == 0 and s['pass_rate'] is None
    assert s['pattern_denominator'] == 10


def test_precise_failure_reasons_and_excluded_numbers():
    result = validate_round(history((1, 3, 12, 20, 30, 41)), 11)
    assert '[1, 3]' in verdict(result, 'F01').reason
    assert '중복 2개' in verdict(result, 'F02').reason
    assert '1: 10' in verdict(result, 'F06').reason
    assert len(dict(result.excluded_numbers)[1]) == 2
    result = validate_round(history((12, 13, 14, 20, 30, 41)), 11)
    assert '10~19' in verdict(result, 'F04').reason
    assert '(12, 13, 14)' in verdict(result, 'F05').reason


def test_bad_older_history_does_not_affect_f02_window():
    rows = history()
    rows[0] = Draw(1, 'invalid', (), 0)
    result = validate_round(rows, 11, ('F02',))
    assert result.overall_passed is True
    assert verdict(result, 'F06').passed is None


def test_summary_known_sample_and_csv():
    # 11회: 모두 통과, 12회: F07만 실패, 13회: F03 실패(독립 두 쌍)
    rows = history() + [draw(12, (2, 4, 6, 8, 10, 12)), draw(13, (12, 13, 20, 21, 30, 41))]
    result = backtest(rows, 11, 13, ('F03', 'F07'))
    assert result.summary['passed'] == 1 and result.summary['failed'] == 2
    assert result.summary['pass_rate'] == 1 / 3
    assert result.summary['patterns'] == dict(none=1, one_pair=1, multiple=1)
    for f in result.summary['filters']:
        if f['filter'] in ('F03', 'F07'):
            assert f['passed'] == 2
    exported = list(csv.DictReader(io.StringIO(rounds_csv(result.rows, 'read-time').decode('utf-8-sig'))))
    assert len(exported) == 3 and exported[0]['수집 확인 시점'] == 'read-time'
    assert exported[1]['F07 판정'] == '실패' and exported[1]['F06 적용'] == 'OFF'
    assert len(list(csv.reader(io.StringIO(summary_csv(result).decode('utf-8-sig'))))) == 9


def test_per_round_cache_excludes_future_keys():
    _validate_snapshot.cache_clear()
    validate_round(history(), 11)
    before = _validate_snapshot.cache_info()
    validate_round(history() + [draw(12)], 11)
    assert _validate_snapshot.cache_info().hits == before.hits + 1
    validate_round(history(), 11, ())
    assert _validate_snapshot.cache_info().misses == before.misses + 1


def test_read_snapshot_is_readonly(tmp_path):
    from database import Database
    db = Database(tmp_path / 'readonly.db')
    for row in history():
        db.save(row)
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    rows, meta = read_snapshot(db.path)
    backtest(rows)
    assert len(rows) == 11 and meta == {}
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
    with pytest.raises(Exception):
        read_snapshot(tmp_path / 'missing.db')
    assert not (tmp_path / 'missing.db').exists()


@pytest.mark.parametrize('start,end', [(0, 11), (12, 11), (1, 12)])
def test_invalid_backtest_ranges(start, end):
    with pytest.raises(ValueError):
        backtest(history(), start, end)


def test_engine_backtest_entrypoint():
    from engine import backtest as engine_backtest
    assert engine_backtest(history()).summary == backtest(history()).summary
