from dataclasses import replace
from datetime import timedelta
import hashlib
from pathlib import Path
import pytest
from streamlit.testing.v1 import AppTest
from backtesting import independent_filter_rates, rate_indicator, RATE_WARNING_TEXT
from settings import FilterSettings, COMBINATION_FILTER_IDS
from validator import Draw, FIRST_DRAW


def history():
    rows = [Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45)
            for i in range(1, 11)]
    for i in range(11, 16):
        combo = (2, 12, 20, 21, 30, 41) if i < 15 else (1, 11, 21, 24, 35, 42)
        rows.append(Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), combo, 45))
    return rows


@pytest.mark.parametrize('passed,total,warning', [(79, 100, True), (4, 5, False), (81, 100, False),
                                               (0, 1, True), (1, 1, False), (799999, 1000000, True)])
def test_exact_eighty_percent_boundary(passed, total, warning):
    stat = dict(passed=passed, evaluable=total, pass_rate=passed / total, unavailable=0)
    indicator = rate_indicator(stat, True)
    assert indicator['warning'] == warning
    assert ('⚠ 주의' in indicator['label']) == warning
    assert indicator['help'] == (RATE_WARNING_TEXT if warning else None)


def test_off_and_unavailable_never_guess_warning():
    for stat in (None, dict(passed=0, evaluable=0, pass_rate=None, unavailable=10)):
        for enabled in (True, False):
            indicator = rate_indicator(stat, enabled)
            assert '분석 불가' in indicator['label']
            assert not indicator['warning'] and indicator['help'] is None
    indicator = rate_indicator(dict(passed=1, evaluable=2, pass_rate=0.5, unavailable=3), False)
    assert 'OFF · 참고용' in indicator['label'] and '분석 불가 3회 제외' in indicator['label']
    assert indicator['warning']


def test_actual_independent_rates_follow_period_values_and_off_flags():
    cfg = FilterSettings()
    rows = history()
    stats = independent_filter_rates(rows, 11, 15, cfg)
    assert tuple(s['filter'] for s in stats) == COMBINATION_FILTER_IDS  # F13 제외
    assert next(s for s in stats if s['filter'] == 'F08')['pass_rate'] == 0.8
    assert next(s for s in independent_filter_rates(rows, 11, 14, cfg) if s['filter'] == 'F08')['pass_rate'] == 1
    assert next(s for s in independent_filter_rates(rows, 11, 15, replace(cfg, end_digit_max=1)) if s['filter'] == 'F08')['pass_rate'] == 0
    off = replace(cfg, enabled_ids=(), pool_size=5, optimization_trials=1)
    assert independent_filter_rates(rows, 11, 15, off) == stats
    early = independent_filter_rates(rows, 1, 1, cfg)
    assert next(s for s in early if s['filter'] == 'F06')['pass_rate'] is None


def test_ui_warning_period_settings_cache_no_full_analysis_and_db_preserved(tmp_path, monkeypatch):
    import database
    import backtesting
    import engine
    db = database.Database(tmp_path / 'rates.db')
    for row in history():
        db.save(row)
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(database, 'Database', lambda: db)
    original, calls = backtesting.independent_filter_rates, []

    def counted(draws, start, end, config):
        calls.append((start, end, config))
        return original(draws, start, end, config)

    def forbid_full_analysis(*args, **kwargs):
        raise AssertionError('통과율 표시로 전체 조합 엔진을 호출하면 안 됩니다.')

    monkeypatch.setattr(backtesting, 'independent_filter_rates', counted)
    monkeypatch.setattr(engine, 'analyze', forbid_full_analysis)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('조건 설정').run()

    def indicators():
        return [m for m in at.markdown if m.value.startswith(('ON ·', 'OFF · 참고용'))]

    assert not at.exception and len(indicators()) == 12 and len(calls) == 1
    assert '80.0000%' in indicators()[7].value and '⚠' not in indicators()[7].value
    assert any('11~15회' in c.value for c in at.caption)
    at.checkbox[7].uncheck().run()
    assert len(calls) == 1 and 'OFF · 참고용' in indicators()[7].value
    next(w for w in at.number_input if w.label == '검증 종료 회차').set_value(14).run()
    assert len(calls) == 2 and '100.0000%' in indicators()[7].value
    next(w for w in at.number_input if w.label == 'F08 같은 끝자리 최대 개수').set_value(1).run()
    assert len(calls) == 3 and '⚠ 주의' in indicators()[7].value
    assert indicators()[7].proto.help == RATE_WARNING_TEXT
    at.checkbox[12].uncheck().run()
    at.run()
    assert len(calls) == 3
    at.sidebar.radio[0].set_value('당첨번호 검증').run()
    assert not at.exception and len(calls) == 3
    assert any('11~14회' in c.value for c in at.caption)
    next(w for w in at.radio if w.label == '검증 방식').set_value('과거 백테스트').run()
    assert next(m for m in at.metric if m.label == '통과 / 판정 가능').value.endswith('/ 4')
    next(w for w in at.number_input if w.label == '검증 시작 회차').set_value(1).run()
    next(w for w in at.number_input if w.label == '검증 종료 회차').set_value(1).run()
    assert not at.exception
    assert '분석 불가' in indicators()[5].value and '⚠' not in indicators()[5].value
    next(w for w in at.number_input if w.label == '검증 시작 회차').set_value(2).run()
    assert not at.exception
    assert all('분석 불가' in m.value and '⚠' not in m.value for m in indicators())
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
