"""SQLite 저장소. 회차별 커밋으로 중단 후 재개를 지원한다."""
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from validator import Draw, validate_draw
from runtime_support import DATA_DIR, ROOT
import json

DEFAULT_DB = DATA_DIR / 'lotto.db'


def seed_draws(path=None):
    """Public draw facts only; validate the entire seed before writing anything."""
    source = Path(path) if path else ROOT / 'seed' / 'draws.json'
    data = json.loads(source.read_text(encoding='utf-8'))
    if data['version'] != 1:
        raise ValueError('지원하지 않는 초기 데이터 형식입니다.')
    draws = [validate_draw(Draw(d['round'], d['draw_date'], tuple(d['numbers']), d['bonus']))
             for d in data['draws']]
    if not draws or [d.round for d in draws] != list(range(1, len(draws) + 1)):
        raise ValueError('초기 데이터의 회차가 연속적이지 않습니다.')
    return draws


class Database:
    def __init__(self, path=DEFAULT_DB):
        self.path = Path(path)
        restore = (not self.path.exists() and self.path.resolve() == DEFAULT_DB.resolve()
                   and (ROOT / 'seed' / 'draws.json').exists())
        initial = seed_draws() if restore else []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            checks = ','.join(f'n{i} INTEGER NOT NULL CHECK(n{i} BETWEEN 1 AND 45)' for i in range(1, 7))
            con.execute(f'CREATE TABLE IF NOT EXISTS draws (round INTEGER PRIMARY KEY CHECK(round > 0), draw_date TEXT NOT NULL, {checks}, bonus INTEGER NOT NULL CHECK(bonus BETWEEN 1 AND 45))')
            con.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            if initial:
                con.executemany('INSERT INTO draws VALUES (?,?,?,?,?,?,?,?,?)',
                                [(d.round, d.draw_date, *d.numbers, d.bonus) for d in initial])
                con.execute('INSERT INTO metadata VALUES (?,?)',
                            ('seed_source', f'배포 초기 데이터 {len(initial)}회 · 최신 여부는 수집으로 확인 필요'))

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=30)
        try:
            with con:
                yield con
        finally:
            con.close()

    def save(self, draw):
        d = validate_draw(draw)
        with self.connect() as con:
            old = con.execute('SELECT * FROM draws WHERE round=?', (d.round,)).fetchone()
            row = (d.round, d.draw_date, *d.numbers, d.bonus)
            if old and old != row:
                raise ValueError(f'{d.round}회 기존 데이터와 충돌합니다.')
            return con.execute('INSERT OR IGNORE INTO draws VALUES (?,?,?,?,?,?,?,?,?)', row).rowcount

    def all(self):
        with self.connect() as con:
            return [Draw(r[0], r[1], tuple(r[2:8]), r[8]) for r in con.execute('SELECT * FROM draws ORDER BY round')]

    def missing(self, latest):
        stored = {d.round for d in self.all()}
        return [r for r in range(1, latest + 1) if r not in stored]

    def set_meta(self, key, value):
        with self.connect() as con:
            con.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, value))

    def get_meta(self, key):
        with self.connect() as con:
            row = con.execute('SELECT value FROM metadata WHERE key=?', (key,)).fetchone()
            return row[0] if row else None
