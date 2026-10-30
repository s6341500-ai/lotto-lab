"""Network-independent regression tests; only official snapshot facts are used."""
import hashlib
import json
from unittest.mock import Mock

import pytest
import requests

import database
from collector import Collector, HISTORY_URL, URL
from database import Database, seed_draws
from validator import Draw


def test_existing_empty_and_partial_default_db_restore(tmp_path, monkeypatch):
    path = tmp_path / 'lotto.db'
    db = Database(path)
    db.set_meta('personal', 'preserved')
    settings = tmp_path / 'settings.json'
    settings.write_bytes(b'{"preserved":true}')
    monkeypatch.setattr(database, 'DEFAULT_DB', path)
    # Explicit path now points to the managed default, including an existing empty file.
    db = Database(path)
    assert db.all() == seed_draws()
    assert db.get_meta('personal') == 'preserved'
    assert settings.read_bytes() == b'{"preserved":true}'
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert Database(path).all() == seed_draws()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_partial_default_fills_gaps_and_keeps_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'partial.db'
    db = Database(path)
    db.save(seed_draws()[7])
    db.set_meta('personal', 'retained')
    monkeypatch.setattr(database, 'DEFAULT_DB', path)
    assert Database(path).all() == seed_draws()
    assert db.get_meta('personal') == 'retained'


def test_conflicting_restore_never_changes_existing_file(tmp_path, monkeypatch):
    path = tmp_path / 'conflict.db'
    db = Database(path)
    db.save(Draw(1, '2002-12-07', (1, 2, 3, 4, 5, 6), 7))
    before = path.read_bytes()
    monkeypatch.setattr(database, 'DEFAULT_DB', path)
    with pytest.raises(ValueError, match='충돌'):
        Database(path)
    assert path.read_bytes() == before


def test_batch_conflict_rolls_back_all_new_rows(tmp_path):
    db = Database(tmp_path / 'atomic.db')
    db.save(seed_draws()[0])
    with pytest.raises(ValueError, match='충돌'):
        db.save_many([seed_draws()[1], Draw(1, '2002-12-07', (1, 2, 3, 4, 5, 6), 7)])
    assert db.all() == seed_draws()[:1]
    with pytest.raises(ValueError, match='중복'):
        db.save_many([seed_draws()[1], seed_draws()[1]])


def payload(draws):
    return {'data': {'list': [dict(ltEpsd=d.round, ltRflYmd=d.draw_date.replace('-', ''),
                                 bnsWnNo=d.bonus,
                                 **{f'tm{i}WnNo': n for i, n in enumerate(d.numbers, 1)})
                                for d in draws]}}


def response(draws):
    r = Mock()
    r.headers = {'Content-Type': 'application/json'}
    r.json.return_value = payload(draws)
    return r


def test_history_endpoint_filters_surrounding_rounds():
    session = Mock()
    session.get.return_value = response(seed_draws()[:10])
    result = Collector(session=session, retries=0).fetch(2)
    assert result == seed_draws()[1:2]
    assert session.get.call_args.args == (HISTORY_URL,)
    assert session.get.call_args.kwargs['params'] == {'srchDir': 'center', 'srchLtEpsd': 2}


def test_incremental_and_same_path_restart_no_full_download(tmp_path):
    db = Database(tmp_path / 'incremental.db')
    db.save_many(seed_draws()[:-1])
    c = Collector()
    c.fetch = Mock(return_value=seed_draws()[-1:])
    assert c.update(db)['ok']
    c.fetch.assert_called_once_with('latest')
    assert db.all() == seed_draws()
    c.fetch.reset_mock()
    result = c.update(Database(db.path))
    assert result['ok'] and result['added'] == 0
    assert result['stored_count'] == 1247 and result['missing'] == []
    c.fetch.assert_called_once_with('latest')


def test_verified_fallback_and_failure_preserve_last_success(tmp_path):
    db = Database(tmp_path / 'fallback.db')
    db.save_many(seed_draws()[:-1])
    session = Mock()
    session.get.side_effect = [requests.Timeout(), response(seed_draws()[-1:])]
    c = Collector(session=session, retries=0, sleep=Mock())
    result = c.update(db)
    assert result['ok'] and result['source'] == HISTORY_URL
    assert [call.args[0] for call in session.get.call_args_list] == [URL, HISTORY_URL]
    last_success = db.get_meta('last_success')
    session.get.side_effect = requests.Timeout('offline')
    assert not c.update(db)['ok']
    assert db.all() == seed_draws()
    assert db.get_meta('last_success') == last_success
    assert json.loads(db.get_meta('update_status'))['stored_count'] == 1247
