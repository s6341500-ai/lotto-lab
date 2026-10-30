from pathlib import Path
from unittest.mock import Mock

from streamlit.testing.v1 import AppTest

from collector import Collector
from database import Database, seed_draws


def test_startup_and_rerun_use_saved_data_only_manual_button_collects(tmp_path, monkeypatch):
    import database

    db = Database(tmp_path / 'manual.db')
    db.save(seed_draws()[0])
    monkeypatch.setattr(database, 'Database', lambda: db)
    update = Mock(return_value={'ok': False, 'message': 'offline'})
    monkeypatch.setattr(Collector, 'update', update)
    app = AppTest.from_file(str(Path(__file__).resolve().parents[1] / 'app.py'))
    app.run(timeout=10)
    assert not app.exception and app.metric[0].value == '1회'
    app.run(timeout=10)
    update.assert_not_called()
    next(b for b in app.button if b.label == '↻ 최신 회차 확인·누락 수집').click().run()
    update.assert_called_once_with(db)
    assert not app.exception
    app.run()
    assert update.call_count == 1
    assert db.all() == seed_draws()[:1]


def test_manual_collection_timeout_and_retry_limits():
    assert Collector().timeout == (3, 5)
    assert Collector().retries == 0
    assert Collector(retries=10).retries == 1
