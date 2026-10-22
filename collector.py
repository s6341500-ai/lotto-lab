"""동행복권 JSON 수집. HTML/차단/스키마 변경은 미발표와 구별한다."""
import argparse
import json
import time
from datetime import datetime
import requests
from database import Database
from validator import Draw, FIRST_DRAW, KST, today_kst, validate_draw

URL = 'https://www.dhlottery.co.kr/lt645/selectPstLt645Info.do'


class CollectionError(RuntimeError):
    pass


def parse_response(payload):
    if not isinstance(payload, dict) or payload.get('resultCode') not in (None, '', 0, '0'):
        raise CollectionError('API 오류 응답입니다.')
    data = payload.get('data')
    if not isinstance(data, dict) or not isinstance(data.get('list'), list):
        raise CollectionError('JSON 구조 변경: data.list 배열이 없습니다.')
    draws = []
    try:
        for row in data['list']:
            raw_date = str(row['ltRflYmd'])
            day = datetime.strptime(raw_date, '%Y%m%d').date().isoformat()
            # API의 정수형만 허용하여 소수/문자열의 묵시적 변환을 막는다.
            draws.append(validate_draw(Draw(row['ltEpsd'], day, tuple(row[f'tm{i}WnNo'] for i in range(1, 7)), row['bnsWnNo'])))
    except (KeyError, TypeError, ValueError) as exc:
        raise CollectionError(f'당첨 데이터 검증 실패: {exc}') from exc
    if len({d.round for d in draws}) != len(draws):
        raise CollectionError('API 응답 내 회차 중복입니다.')
    return draws


class Collector:
    def __init__(self, session=None, interval=0.8, retries=2, timeout=15, sleep=time.sleep):
        self.session = session or requests.Session()
        self.interval = max(0.5, interval)
        self.retries = min(5, max(0, retries))
        self.timeout = timeout
        self.sleep = sleep
        self.last_request = None

    def fetch(self, round_no='all'):
        for attempt in range(self.retries + 1):
            if self.last_request is not None:
                self.sleep(max(0, self.interval - (time.monotonic() - self.last_request)))
            self.last_request = time.monotonic()
            try:
                response = self.session.get(URL, params={'srchLtEpsd': round_no}, timeout=self.timeout,
                                            headers={'Accept': 'application/json', 'User-Agent': 'LottoLab/1.0'})
                response.raise_for_status()
                if 'json' not in response.headers.get('Content-Type', '').lower():
                    raise CollectionError(f'JSON 대신 {response.headers.get("Content-Type", "알 수 없는 형식")} 응답: 차단 또는 주소 변경 가능')
                return parse_response(response.json())
            except (requests.RequestException, ValueError, CollectionError) as exc:
                if attempt == self.retries:
                    raise CollectionError(f'{round_no} 조회 실패 ({attempt + 1}회 시도): {exc}') from exc
                self.sleep(2 ** attempt)

    def update(self, db):
        started = datetime.now(KST).isoformat(timespec='seconds')
        added = 0
        try:
            # 실제 검증한 all 조회로 최신 회차를 찾는다. 무한 순차 탐색을 하지 않는다.
            draws = self.fetch()
            if not draws:
                raise CollectionError('전체 조회가 비어 있어 최신 발표 회차를 확인할 수 없습니다.')
            latest = max(d.round for d in draws)
            for draw in sorted(draws, key=lambda d: d.round):
                added += db.save(draw)
            # 전체 응답에 누락된 회차만 제한된 범위에서 재조회한다.
            for number in db.missing(latest):
                rows = self.fetch(number)
                if len(rows) != 1 or rows[0].round != number:
                    raise CollectionError(f'{number}회 누락: 과거 회차의 빈 응답은 미발표로 취급하지 않습니다.')
                added += db.save(rows[0])
            # 달력은 상한/지연 감지에만 사용하며 최신 발표의 증거로 쓰지 않는다.
            expected = max(0, (today_kst() - FIRST_DRAW).days // 7 + 1)
            if latest < expected:
                raise CollectionError(f'최신 응답은 {latest}회입니다. {latest + 1}회 이후는 미발표 또는 게시 지연으로 최신성 확인이 필요합니다.')
            result = {'ok': True, 'checked_at': started, 'added': added, 'latest': latest,
                      'message': f'{latest}회까지 저장 완료 · 새로 저장 {added}회 · 누락 0회'}
        except (CollectionError, ValueError) as exc:
            result = {'ok': False, 'checked_at': started, 'added': added, 'message': str(exc)}
        db.set_meta('update_status', json.dumps(result, ensure_ascii=False))
        return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=None)
    args = parser.parse_args()
    result = Collector().update(Database(args.db) if args.db else Database())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result['ok'] else 1)
