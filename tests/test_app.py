from pathlib import Path
from streamlit.testing.v1 import AppTest
from database import Database
from validator import Draw
from datetime import timedelta
from validator import FIRST_DRAW


def test_dashboard_and_menus(tmp_path, monkeypatch):
    import database
    db = Database(tmp_path / 'ui.db')
    db.save(Draw(1, '2002-12-07', (10, 23, 29, 33, 37, 40), 16))
    monkeypatch.setattr(database, 'Database', lambda: db)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run(timeout=20)
    assert not at.exception
    assert at.metric[0].value == '1회'
    assert len(at.dataframe) == 1
    for menu in ['조건 설정', '번호 추출', '당첨번호 검증']:
        at.sidebar.radio[0].set_value(menu).run()
        assert not at.exception
        assert at.title[0].value == menu


def test_analysis_button_cache_stale_and_toggle_persistence(tmp_path, monkeypatch):
    import database
    import engine
    db = Database(tmp_path / 'analysis_ui.db')
    for i in range(1, 11):
        db.save(Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45))
    monkeypatch.setattr(database, 'Database', lambda: db)
    actual = engine.analyze
    calls = []

    def small_analysis(ctx, enabled, seed):
        calls.append(enabled)
        return actual(ctx, enabled, seed, universe=(2, 4, 6, 8, 12, 14, 16, 18, 20, 21, 24, 27, 30, 31, 35, 40))

    monkeypatch.setattr(engine, 'analyze', small_analysis)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('번호 추출').run()
    assert not at.exception and calls == []
    at.button[0].click().run(timeout=20)
    assert not at.exception and len(calls) == 1
    assert any(t.value == '최종 5조합' for t in at.subheader)
    at.run()
    assert len(calls) == 1
    at.button[0].click().run()
    assert len(calls) == 1  # 동일 요청 캐시
    at.checkbox[2].uncheck().run()
    assert len(calls) == 1
    assert not any(t.value == '최종 5조합' for t in at.subheader)
    at.sidebar.radio[0].set_value('대시보드').run()
    at.sidebar.radio[0].set_value('조건 설정').run()
    assert at.checkbox[2].value is False
    at.button[0].click().run(timeout=20)
    assert not at.exception and len(calls) == 2
    assert 'F03' not in calls[-1]


def test_validation_backtest_changes_precision_and_readonly(tmp_path, monkeypatch):
    import hashlib
    import database
    import engine
    import backtesting
    db = Database(tmp_path / 'backtest_ui.db')
    for i in range(1, 11):
        db.save(Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45))
    for i, combo in [(11, (2, 12, 20, 21, 30, 41)), (12, (2, 12, 20, 24, 30, 40)), (13, (12, 13, 20, 21, 30, 41))]:
        db.save(Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), combo, 45))
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(database, 'Database', lambda: db)
    original_analyze, original_backtest = engine.analyze, backtesting.backtest
    precision_calls, backtest_calls = [], []

    def small_analyze(context, enabled, seed):
        precision_calls.append(context)
        return original_analyze(context, enabled, seed, universe=range(1, 16))

    def counted_backtest(draws, start, end, enabled):
        backtest_calls.append((start, end, enabled))
        return original_backtest(draws, start, end, enabled)

    monkeypatch.setattr(engine, 'analyze', small_analyze)
    monkeypatch.setattr(backtesting, 'backtest', counted_backtest)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('당첨번호 검증').run()
    assert not at.exception and precision_calls == []
    next(w for w in at.selectbox if w.label == '검증 대상 회차').set_value(11).run()
    assert any('전체 통과' in s.value for s in at.success)
    assert '3:2' in at.caption[1].value or any('3:2' in c.value for c in at.caption)
    at.button[0].click().run(timeout=20)
    assert not at.exception and len(precision_calls) == 1
    assert precision_calls[0].basis_round == 10
    assert max(precision_calls[0].recent_rounds) == 10
    next(w for w in at.radio if w.label == '검증 방식').set_value('과거 백테스트').run()
    assert not at.exception and len(backtest_calls) == 1
    assert backtest_calls[0][:2] == (11, 13)
    at.run()
    assert len(backtest_calls) == 1
    at.checkbox[2].uncheck().run()
    assert not at.exception and len(backtest_calls) == 2
    assert 'F03' not in backtest_calls[-1][2]
    next(w for w in at.number_input if w.label == '검증 시작 회차').set_value(1).run()
    assert not at.exception
    assert next(m for m in at.metric if m.label == '분석 불가 (분모 제외)').value == '10'
    assert len(precision_calls) == 1  # 필터 변경으로 무거운 계산을 자동 실행하지 않음
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before


def test_settings_presets_confirmation_restore_and_f13_cache(tmp_path, monkeypatch):
    import database
    import engine
    import hashlib
    from settings import SettingsStore
    db = Database(tmp_path / 'presets_ui.db')
    for i in range(1, 11):
        db.save(Draw(i, (FIRST_DRAW + timedelta(weeks=i - 1)).isoformat(), (1, 3, 5, 7, 9, 11), 45))
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    monkeypatch.setattr(database, 'Database', lambda: db)
    actual, calls = engine.analyze, []

    def small(ctx, enabled, seed):
        calls.append(ctx)
        return actual(ctx, enabled, seed, universe=(2, 4, 6, 8, 12, 14, 16, 18, 20, 21, 24, 27, 30, 31, 35, 40))

    monkeypatch.setattr(engine, 'analyze', small)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('조건 설정').run()
    assert len(at.checkbox) == 13 and all(c.value for c in at.checkbox)
    next(w for w in at.number_input if w.label == 'F09 인접 최대 간격').set_value(17).run()
    at.text_input[0].set_value('시험').run()
    next(b for b in at.button if b.label == '사용자 프리셋 저장').click().run()
    store = SettingsStore(tmp_path / 'settings.json')
    assert store.load_preset('시험').max_gap == 17
    next(b for b in at.button if b.label == '현재 설정을 기본값으로 저장').click().run()
    next(w for w in at.number_input if w.label == 'F09 인접 최대 간격').set_value(18).run()
    next(b for b in at.button if b.label == '저장 기본값 복원').click().run()
    assert next(w for w in at.number_input if w.label == 'F09 인접 최대 간격').value == 17
    next(b for b in at.button if b.label == '2단계 재현 설정 (F08~F13 OFF)').click().run()
    assert all(not at.checkbox[i].value for i in range(7, 13))
    next(b for b in at.button if b.label == '선택 프리셋 불러오기').click().run()
    assert all(at.checkbox[i].value for i in range(13))
    deletion = next(b for b in at.button if b.label == '선택 프리셋 삭제')
    assert deletion.disabled
    next(c for c in at.checkbox if '프리셋 삭제 확인' in c.label).check().run()
    next(b for b in at.button if b.label == '선택 프리셋 삭제').click().run()
    assert store.read()['presets'] == {}
    assert store.default().max_gap == 17
    next(b for b in at.button if b.label == '전체 조합 분석 및 5조합 추출').click().run(timeout=20)
    assert not at.exception and len(calls) == 1
    at.checkbox[12].uncheck().run()
    next(b for b in at.button if b.label == '전체 조합 분석 및 5조합 추출').click().run(timeout=20)
    assert not at.exception and len(calls) == 1  # F13 변경은 후보 재순회 없음
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before
