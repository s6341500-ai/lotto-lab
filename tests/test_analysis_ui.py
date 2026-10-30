from datetime import timedelta
import hashlib
from pathlib import Path
from streamlit.testing.v1 import AppTest
from database import Database
from validator import Draw, FIRST_DRAW
from experiments import ExperimentStore


def setup_app(tmp_path, monkeypatch):
    import database
    db = Database(tmp_path / 'analysis.db')
    for r in range(1, 15):
        combo = (1, 3, 5, 7, 9, 11) if r <= 10 else (2, 12, 20, 21, 30, 41) if r == 11 else (2, 4, 6, 8, 10, 12) if r == 12 else (12, 13, 20, 21, 30, 41) if r == 13 else (2, 3, 4, 6, 8, 12)
        db.save(Draw(r, (FIRST_DRAW + timedelta(weeks=r - 1)).isoformat(), combo, 45))
    monkeypatch.setattr(database, 'Database', lambda: db)
    at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    at.session_state['startup_checked'] = True
    at.run()
    at.sidebar.radio[0].set_value('필터 분석').run(timeout=20)
    return at, db


def button(at, label):
    return next(w for w in at.button if w.label == label)


def test_filter_analysis_buttons_stale_presets_records_and_db(tmp_path, monkeypatch):
    import engine

    def forbid(*args, **kwargs):
        raise AssertionError('기본 영향도/A-B 분석에서 전체 조합 엔진을 호출하면 안 됩니다.')

    monkeypatch.setattr(engine, 'analyze', forbid)
    at, db = setup_app(tmp_path, monkeypatch)
    before = hashlib.sha256(db.path.read_bytes()).hexdigest()
    assert not at.exception
    assert [t.label for t in at.tabs] == ['조건별 영향도', 'A-B 비교', '실험 기록']
    assert not any(s.value == '영향도 결과' for s in at.subheader)
    button(at, '조건별 영향도 분석 실행').click().run(timeout=20)
    assert not at.exception and any(s.value == '영향도 결과' for s in at.subheader)
    next(w for w in at.text_input if w.key == 'lab_impact_name').set_value('기준실험').run()
    next(w for w in at.button if w.key == 'lab_impact_save').click().run()
    store = ExperimentStore(tmp_path / 'experiments.json')
    assert len(store.read()['experiments']) == 1
    next(w for w in at.checkbox if w.label.startswith('B F06 ')).uncheck().run()
    button(at, 'A/B 비교 실행').click().run(timeout=20)
    assert not at.exception and any(s.value == 'A/B 비교 결과' for s in at.subheader)
    assert 'lab_precision_result' not in at.session_state
    assert not any('후보 수' in frame.value.columns for frame in at.dataframe)
    next(w for w in at.text_input if w.key == 'lab_ab_name').set_value('AB실험').run()
    next(w for w in at.button if w.key == 'lab_ab_save').click().run()
    records = store.read()['experiments']
    assert len(records) == 2
    ab_id = next(k for k, v in records.items() if v['name'] == 'AB실험')
    next(w for w in at.number_input if w.label == 'B F12 합계 최소').set_value(150).run()
    assert not any(s.value == 'A/B 비교 결과' for s in at.subheader)
    next(w for w in at.number_input if w.label == '검증 종료 회차').set_value(13).run()
    assert not any(s.value == '영향도 결과' for s in at.subheader)
    next(w for w in at.selectbox if w.label == '저장 실험 열기').set_value(ab_id).run()
    button(at, '저장 설정·기간을 A/B에 불러오기').click().run()
    assert not at.exception
    assert next(w for w in at.number_input if w.label == '검증 종료 회차').value == 14
    assert next(w for w in at.number_input if w.label == 'B F12 합계 최소').value == 100
    assert button(at, '선택 실험 삭제').disabled
    next(w for w in at.checkbox if 'AB실험' in w.label and '삭제 확인' in w.label).check().run()
    button(at, '선택 실험 삭제').click().run()
    assert not at.exception and len(store.read()['experiments']) == 1
    assert hashlib.sha256(db.path.read_bytes()).hexdigest() == before


def test_precision_explicit_button_same_basis_and_invalidation(tmp_path, monkeypatch):
    import engine
    actual, calls = engine.analyze, []

    def small(ctx, enabled, seed):
        calls.append(ctx)
        return actual(ctx, enabled, seed, universe=range(1, 16))

    monkeypatch.setattr(engine, 'analyze', small)
    at, db = setup_app(tmp_path, monkeypatch)
    next(w for w in at.checkbox if w.label.startswith('B F06 ')).uncheck().run()
    button(at, 'A/B 비교 실행').click().run(timeout=20)
    assert not at.exception and calls == []
    button(at, 'A/B 전체 조합 정밀 비교 실행').click().run(timeout=20)
    assert not at.exception and len(calls) == 2
    assert calls[0].basis_round == calls[1].basis_round == 13
    assert any('후보 수' in frame.value.columns for frame in at.dataframe)
    at.run()
    assert len(calls) == 2
    next(w for w in at.selectbox if w.label == 'A/B 정밀 비교 대상 회차').set_value(13).run()
    assert not any('후보 수' in frame.value.columns for frame in at.dataframe)
    assert len(calls) == 2
