from datetime import date
from unittest.mock import Mock
import pytest
import requests
from collector import Collector, CollectionError, parse_response
from database import Database
from validator import Draw, validate_draw


def draw(number=1):
    from validator import FIRST_DRAW
    from datetime import timedelta
    return Draw(number, (FIRST_DRAW + timedelta(weeks=number - 1)).isoformat(), (10, 23, 29, 33, 37, 40), 16)


@pytest.mark.parametrize('numbers,bonus', [((1, 2, 3, 4, 5), 6), ((1, 1, 3, 4, 5, 6), 7), ((0, 2, 3, 4, 5, 6), 7), ((1, 2, 3, 4, 5, 46), 7), ((1, 2, 3, 4, 5, 6), 6), ((True, 2, 3, 4, 5, 6), 7)])
def test_invalid_numbers(numbers, bonus):
    with pytest.raises(ValueError):
        validate_draw(Draw(1, '2002-12-07', numbers, bonus))


def test_valid_and_dates():
    assert validate_draw(draw()) == draw()
    with pytest.raises(ValueError):
        validate_draw(Draw(1, '2002-12-08', draw().numbers, 16))
    with pytest.raises(ValueError):
        validate_draw(draw(), today=date(2002, 12, 6))


def test_database_duplicate_conflict_and_missing(tmp_path):
    db = Database(tmp_path / 'test.db')
    assert db.save(draw()) == 1
    assert db.save(draw()) == 0
    with pytest.raises(ValueError):
        db.save(Draw(1, '2002-12-07', (1, 2, 3, 4, 5, 6), 7))
    db.save(draw(3))
    assert db.missing(3) == [2]
    assert len(Database(db.path).all()) == 2


@pytest.mark.parametrize('payload', [{}, {'data': {}}, {'resultCode': 'ERROR', 'data': {'list': []}}, {'data': {'list': [{}]}}])
def test_bad_schema(payload):
    with pytest.raises(CollectionError):
        parse_response(payload)


def test_timeout_retries_preserve_data(tmp_path):
    db = Database(tmp_path / 'test.db')
    db.save(draw())
    session = Mock()
    session.get.side_effect = requests.Timeout('timeout')
    collector = Collector(session=session, sleep=Mock())
    result = collector.update(db)
    assert not result['ok']
    assert session.get.call_count == 6  # Primary and verified historical fallback.
    assert db.all() == [draw()]
    assert '조회 실패' in result['message']


def test_html_is_not_unpublished():
    session = Mock()
    session.get.return_value.headers = {'Content-Type': 'text/html'}
    with pytest.raises(CollectionError, match='JSON 대신'):
        Collector(session=session, retries=0).fetch(9999)


def test_empty_past_round_fails_and_resume(tmp_path, monkeypatch):
    import collector as module
    monkeypatch.setattr(module, 'today_kst', lambda: date(2002, 12, 21))
    db = Database(tmp_path / 'test.db')
    c = Collector()
    c.fetch = Mock(side_effect=[[draw(1), draw(3)], []])
    assert not c.update(db)['ok']
    assert db.missing(3) == [2]
    c.fetch = Mock(side_effect=[[draw(1), draw(3)], [draw(2)]])
    result = c.update(db)
    assert result['ok'] and result['added'] == 1
    assert db.missing(3) == []
    assert c.fetch.call_args_list[1].args == (2,)


def test_wrong_round_rejected(tmp_path):
    db = Database(tmp_path / 'test.db')
    c = Collector()
    c.fetch = Mock(side_effect=[[draw(3)], [draw(2)]])
    assert not c.update(db)['ok']
    assert db.missing(3) == [1, 2]


def test_empty_all_is_error(tmp_path):
    c = Collector()
    c.fetch = Mock(return_value=[])
    assert not c.update(Database(tmp_path / 'test.db'))['ok']
