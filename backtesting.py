"""실제 당첨번호 검증. 대상 회차를 학습 창에서 제거한 스냅샷만 캐시한다."""
from collections import Counter
from dataclasses import dataclass, asdict, replace
from functools import lru_cache
import csv
import io
import json
from pathlib import Path
import sqlite3
import time

from database import DEFAULT_DB
from filters import (FILTER_IDS, build_context, consecutive_pairs, classify_consecutive,
                     f01, f02, f03, f04, f05, f06, f07)
from filters import f08, f09, f10, f11, f12, select_similarity_history, similarity_index
from settings import FilterSettings, COMBINATION_FILTER_IDS, ALL_FILTER_IDS
from validator import Draw, validate_draw

PATTERN_LABELS = {'none': '연속 없음', 'one_pair': '정확히 1쌍', 'multiple': '2쌍 이상'}


def normalize_enabled(enabled):
    enabled = frozenset(enabled)
    if enabled - set(ALL_FILTER_IDS):
        raise ValueError('알 수 없는 필터입니다.')
    return tuple(fid for fid in COMBINATION_FILTER_IDS if fid in enabled)


@dataclass(frozen=True)
class FilterVerdict:
    filter_id: str
    enabled: bool
    passed: bool | None
    reason: str


@dataclass(frozen=True)
class RoundValidation:
    round: int
    draw_date: str
    numbers: tuple[int, ...]
    history_rounds: tuple[int, ...]
    filters: tuple[FilterVerdict, ...]
    overall_passed: bool | None
    consecutive_pattern: str | None
    pair_count: int | None
    excluded_numbers: tuple[tuple[int, tuple[str, ...]], ...]
    unavailable_reason: str = ''
    settings: FilterSettings | None = None

    @property
    def failed_filters(self):
        return tuple(v.filter_id for v in self.filters if v.enabled and v.passed is False)


@dataclass(frozen=True)
class BacktestResult:
    start: int
    end: int
    enabled: tuple[str, ...]
    rows: tuple[RoundValidation, ...]
    elapsed_seconds: float
    settings: FilterSettings | None = None

    @property
    def summary(self):
        evaluable = sum(r.overall_passed is not None for r in self.rows)
        passed = sum(r.overall_passed is True for r in self.rows)
        filters = []
        for fid in (COMBINATION_FILTER_IDS if self.settings else FILTER_IDS):
            verdicts = [v for r in self.rows for v in r.filters if v.filter_id == fid]
            denominator = sum(v.passed is not None for v in verdicts)
            numerator = sum(v.passed is True for v in verdicts)
            filters.append({'filter': fid, 'enabled': fid in self.enabled, 'evaluable': denominator,
                            'passed': numerator, 'failed': denominator - numerator,
                            'unavailable': len(verdicts) - denominator,
                            'pass_rate': numerator / denominator if denominator else None})
        patterns = Counter(r.consecutive_pattern for r in self.rows if r.consecutive_pattern is not None)
        return {'requested': len(self.rows), 'evaluable': evaluable, 'passed': passed,
                'failed': evaluable - passed, 'unavailable': len(self.rows) - evaluable,
                'pass_rate': passed / evaluable if evaluable else None,
                'filters': filters, 'pattern_denominator': sum(patterns.values()),
                'patterns': {p: patterns[p] for p in PATTERN_LABELS}}


def _snapshot(draws, target_round, lookback=10):
    if type(target_round) is not int or target_round < 1:
        raise ValueError('검증 회차는 양의 정수여야 합니다.')
    # 미래 데이터는 검증/중복 검사/캐시 키에도 넣지 않는다.
    first = max(1, target_round - lookback) if lookback is not None else 1
    relevant = [d for d in draws if first <= d.round <= target_round]
    if len({d.round for d in relevant}) != len(relevant):
        raise ValueError('검증 창에 중복 회차가 있습니다.')
    target = next((d for d in relevant if d.round == target_round), None)
    prior = tuple(sorted((d for d in relevant if d.round < target_round), key=lambda d: d.round))
    return target, prior


def validate_round(draws, target_round, enabled=FILTER_IDS, settings=None):
    config = enabled if isinstance(enabled, FilterSettings) else settings
    normalized = normalize_enabled(config if config else enabled)
    if config is None and set(normalized) - set(FILTER_IDS):
        config = FilterSettings(enabled_ids=normalized)
    lookback = (None if config.history_scope == 'all' else max(10, config.history_n)) if config else 10
    target, prior = _snapshot(draws, target_round, lookback)
    return _validate_snapshot(target_round, target, prior, normalized, config)


@lru_cache(maxsize=8192)
def _validate_snapshot(target_round, target, prior, enabled, config=None):
    ids = COMBINATION_FILTER_IDS if config else FILTER_IDS
    if target is None:
        reason = '대상 회차 데이터가 저장되어 있지 않습니다.'
    else:
        try:
            target = validate_draw(target)
            reason = ''
        except ValueError as exc:
            reason = f'대상 당첨 데이터 오류: {exc}'
    history_rounds = tuple(d.round for d in prior)
    if reason:
        return RoundValidation(target_round, '', (), history_rounds,
                               tuple(FilterVerdict(fid, fid in enabled, None, reason) for fid in ids),
                               None, None, None, (), reason, config)
    combo = target.numbers
    verdicts, exclusions = [], {}
    # OFF 조건도 독립 판정하되 전체 통과 여부에서는 제외한다.
    contexts, context_errors = {}, {}
    for fid in ('F01', 'F02', 'F06'):
        try:
            lookback = {'F01': 2, 'F02': 1, 'F06': 10}[fid]
            window = tuple(d for d in prior if d.round >= target_round - lookback)
            contexts[fid] = build_context(window, (fid,), target_round - 1)
        except ValueError as exc:
            context_errors[fid] = f'분석 불가: {exc}'
    pair_count = consecutive_pairs(combo)
    odds = sum(n % 2 for n in combo)
    buckets = Counter(n // 10 for n in combo)
    triples = [combo[i:i + 3] for i in range(4) if combo[i + 1] == combo[i] + 1 and combo[i + 2] == combo[i] + 2]
    if config:
        try:
            similarity_history = select_similarity_history(prior, target_round, config)
            sim_index = similarity_index(similarity_history, config.similarity_threshold, 'masks')
        except ValueError as exc:
            context_errors['F11'] = f'분석 불가: {exc}'
    for fid in ids:
        active = fid in enabled
        if fid in context_errors:
            verdicts.append(FilterVerdict(fid, active, None, context_errors[fid]))
            continue
        if fid == 'F01':
            ctx = contexts[fid]
            offending = sorted(set(combo) & ctx.f01_excluded)
            passed = f01(combo, ctx.f01_excluded)
            detail = f'직전 2회({target_round - 2}~{target_round - 1}회) 공통 제외 {sorted(ctx.f01_excluded)}; 당첨번호 중 해당 번호 {offending}'
            if active:
                for n in ctx.f01_excluded:
                    exclusions.setdefault(n, []).append('F01: 직전 2회 공통 등장')
        elif fid == 'F02':
            overlap = sorted(set(combo) & contexts[fid].previous)
            passed = f02(combo, contexts[fid].previous)
            detail = f'직전 {target_round - 1}회와 중복 {len(overlap)}개 {overlap}; 최대 1개 허용'
        elif fid == 'F03':
            passed = f03(combo)
            detail = f'연속쌍 {pair_count}개 ({PATTERN_LABELS[classify_consecutive(combo)]}); 0~1쌍만 적격. 3:2는 5조합 전체 구성 규칙'
        elif fid == 'F04':
            passed = f04(combo)
            over = {('1~9', '10~19', '20~29', '30~39', '40~45')[b]: count for b, count in buckets.items() if count > 2}
            detail = f'구간별 최대 2개; 초과 구간 {over or "없음"}'
        elif fid == 'F05':
            passed = f05(combo)
            detail = f'3개 이상 연속 금지; 발견된 연속 3개 {triples or "없음"}'
        elif fid == 'F06':
            ctx = contexts[fid]
            frequencies = dict(ctx.frequencies)
            offending = {n: frequencies[n] for n in combo if n in ctx.f06_excluded}
            passed = f06(combo, ctx.f06_excluded)
            detail = f'직전 10회({target_round - 10}~{target_round - 1}회) 2회 이상 출현한 실제 당첨번호와 출현 횟수 {offending or "없음"}'
            if active:
                for n in ctx.f06_excluded:
                    exclusions.setdefault(n, []).append(f'F06: 직전 10회 {frequencies[n]}회 출현')
        elif fid == 'F07':
            passed = f07(combo)
            detail = f'홀수:짝수 = {odds}:{6 - odds}; 2:4, 3:3, 4:2 허용'
        elif fid == 'F08':
            passed = f08(combo, config.end_digit_max)
            detail = f'끝자리별 개수 {dict(sorted(Counter(n % 10 for n in combo).items()))}; 최대 {config.end_digit_max}개'
        elif fid == 'F09':
            gaps = tuple(b - a for a, b in zip(combo, combo[1:]))
            passed = f09(combo, config.max_gap)
            detail = f'인접 간격 {gaps}; 최대 간격 {max(gaps)} ≤ {config.max_gap} (바깥쪽 간격 제외)'
        elif fid == 'F10':
            low = sum(n <= config.low_boundary for n in combo)
            passed = f10(combo, config.low_boundary, config.low_min, config.low_max, config.high_min, config.high_max)
            detail = f'저번호 1~{config.low_boundary}: {low}개 ({config.low_min}~{config.low_max} 허용), 고번호 {config.low_boundary + 1}~45: {6-low}개 ({config.high_min}~{config.high_max} 허용)'
        elif fid == 'F11':
            passed = f11(combo, sim_index)
            matches = [(d.round, sorted(set(combo) & set(d.numbers))) for d in similarity_history
                       if len(set(combo) & set(d.numbers)) >= config.similarity_threshold] if not passed else []
            scope = '전체 과거' if config.history_scope == 'all' else f'최근 {config.history_n}회'
            detail = f'대상 이전 {scope} 이력 {len(similarity_history)}회, 중복 임계값 {config.similarity_threshold}; 유사 회차/공통 번호 {matches or "없음"}'
        else:
            passed = f12(combo, config.sum_min, config.sum_max)
            detail = f'번호 합계 {sum(combo)}; 허용 {config.sum_min}~{config.sum_max}'
        verdicts.append(FilterVerdict(fid, active, passed, detail))
    unavailable = [v for v in verdicts if v.enabled and v.passed is None]
    overall = None if unavailable else all(v.passed for v in verdicts if v.enabled)
    return RoundValidation(target_round, target.draw_date, combo, history_rounds, tuple(verdicts), overall,
                           classify_consecutive(combo), pair_count,
                           tuple((n, tuple(why)) for n, why in sorted(exclusions.items())),
                           '; '.join(f'{v.filter_id}: {v.reason}' for v in unavailable), config)


def _calculate_backtest(draws, start=11, end=None, enabled=FILTER_IDS, settings=None):
    draws = tuple(draws)
    if not draws:
        raise ValueError('백테스트할 저장 데이터가 없습니다.')
    end = max(d.round for d in draws) if end is None else end
    if type(start) is not int or type(end) is not int or not 1 <= start <= end:
        raise ValueError('시작/종료 회차 범위를 확인하세요.')
    if end > max(d.round for d in draws):
        raise ValueError('종료 회차가 최신 저장 회차보다 큽니다.')
    config = enabled if isinstance(enabled, FilterSettings) else settings
    enabled = normalize_enabled(config if config else enabled)
    if config is None and set(enabled) - set(FILTER_IDS):
        config = FilterSettings(enabled_ids=enabled)
    started = time.perf_counter()
    rows = tuple(validate_round(draws, r, config if config else enabled) for r in range(start, end + 1))
    return BacktestResult(start, end, enabled, rows, time.perf_counter() - started, config)


def backtest(draws, start=11, end=None, enabled=FILTER_IDS, settings=None):
    return _calculate_backtest(draws, start, end, enabled, settings)


RATE_WARNING_TEXT = '과거 실제 당첨번호 통과율이 80% 미만인 조건입니다. 당첨번호를 많이 제외할 수 있으므로 주의하세요.'


def independent_filter_rates(draws, start, end, settings):
    """ON/OFF 및 F13 탐색 설정과 무관한 원래 조건의 독립 통과율."""
    canonical = replace(settings, enabled_ids=(), pool_size=256, optimization_trials=64)
    return _calculate_backtest(draws, start, end, canonical).summary['filters']


def rate_indicator(stat, enabled):
    """반올림된 표시값 대신 정수 분자/분모로 80% 경계를 판정한다."""
    state = 'ON' if enabled else 'OFF · 참고용'
    if not stat or not stat['evaluable'] or stat['pass_rate'] is None:
        return {'label': f'{state} · 분석 불가', 'warning': False, 'help': None}
    warning = stat['passed'] * 5 < stat['evaluable'] * 4
    label = f"{state} · 독립 통과율 {stat['pass_rate']:.4%} ({stat['passed']}/{stat['evaluable']}회)"
    if stat['unavailable']:
        label += f" · 분석 불가 {stat['unavailable']}회 제외"
    if warning:
        label += ' · ⚠ 주의'
    return {'label': label, 'warning': warning, 'help': RATE_WARNING_TEXT if warning else None}


def comparison_context(draws, target_round, enabled=FILTER_IDS, settings=None):
    """선택 회차 전체 조합 비교용. 대상 번호를 문맥 계산에 절대 전달하지 않는다."""
    config = enabled if isinstance(enabled, FilterSettings) else settings
    lookback = (None if config.history_scope == 'all' else max(10, config.history_n)) if config else 10
    target, prior = _snapshot(draws, target_round, lookback)
    if target is None:
        raise ValueError('대상 회차 데이터가 없습니다.')
    enabled = normalize_enabled(config if config else enabled)
    required = 10 if 'F06' in enabled else 2 if 'F01' in enabled else 1
    if not config or 'F11' not in enabled:
        prior = tuple(d for d in prior if d.round >= target_round - required)
    return build_context(prior, enabled, target_round - 1, settings=config, history_before=target_round)


def round_records(rows):
    records = []
    for r in rows:
        row = {'회차': r.round, '추첨일': r.draw_date, '당첨번호': ' · '.join(map(str, r.numbers)),
               '참조 회차': ','.join(map(str, r.history_rounds)),
               '전체 판정': '분석 불가' if r.overall_passed is None else '통과' if r.overall_passed else '실패',
               '연속번호': PATTERN_LABELS.get(r.consecutive_pattern, '분석 불가'),
               '연속쌍 수': r.pair_count, '실패 조건': '/'.join(r.failed_filters), '분석 불가 사유': r.unavailable_reason}
        row['설정 스냅샷'] = (r.settings or FilterSettings(enabled_ids=tuple(v.filter_id for v in r.filters if v.enabled))).snapshot()
        row['F13 분류'] = '추출 최적화 (당첨번호 판정 대상 아님)'
        for v in r.filters:
            row[f'{v.filter_id} 적용'] = 'ON' if v.enabled else 'OFF'
            row[f'{v.filter_id} 판정'] = '분석 불가' if v.passed is None else '통과' if v.passed else '실패'
            row[f'{v.filter_id} 사유'] = v.reason
        records.append(row)
    return records


def rounds_csv(rows, collected_at=''):
    records = [{'수집 확인 시점': collected_at, **row} for row in round_records(rows)]
    out = io.StringIO(newline='')
    if records:
        writer = csv.DictWriter(out, fieldnames=list(records[0]))
        writer.writeheader()
        writer.writerows(records)
    return out.getvalue().encode('utf-8-sig')


def summary_csv(result):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['시작', '종료', '조건', '적용', '판정 가능', '통과', '실패', '분석 불가', '통과율', '설정 스냅샷'])
    snapshot = (result.settings or FilterSettings(enabled_ids=result.enabled)).snapshot()
    s = result.summary
    writer.writerow([result.start, result.end, '활성 조건 전체', '/'.join(result.enabled), s['evaluable'], s['passed'], s['failed'], s['unavailable'], s['pass_rate'], snapshot])
    for f in s['filters']:
        writer.writerow([result.start, result.end, f['filter'], f['enabled'], f['evaluable'], f['passed'], f['failed'], f['unavailable'], f['pass_rate'], snapshot])
    return out.getvalue().encode('utf-8-sig')


def read_snapshot(path=DEFAULT_DB):
    """SQLite mode=ro: 검증/보고서 경로에서는 테이블 생성과 쓰기를 하지 않는다."""
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True)
    try:
        rows = con.execute('SELECT * FROM draws ORDER BY round').fetchall()
        meta = con.execute('SELECT value FROM metadata WHERE key=?', ('update_status',)).fetchone()
        return tuple(Draw(r[0], r[1], tuple(r[2:8]), r[8]) for r in rows), json.loads(meta[0]) if meta else {}
    finally:
        con.close()


if __name__ == '__main__':
    import argparse
    import hashlib
    parser = argparse.ArgumentParser(description='DB 읽기 전용 실제 당첨번호 백테스트')
    parser.add_argument('--start', type=int, default=11)
    parser.add_argument('--end', type=int)
    parser.add_argument('--disable', nargs='*', choices=ALL_FILTER_IDS, default=[])
    parser.add_argument('--legacy', action='store_true', help='F01~F07만 ON')
    parser.add_argument('--config', help='FilterSettings JSON 파일')
    parser.add_argument('--output', default='data/phase4_backtest.json')
    args = parser.parse_args()
    fingerprint = hashlib.sha256(DEFAULT_DB.read_bytes()).hexdigest()
    draws, status = read_snapshot()
    try:
        config = FilterSettings.from_dict(json.loads(Path(args.config).read_text(encoding='utf-8'))) if args.config else (FilterSettings.legacy() if args.legacy else FilterSettings())
        config = replace(config, enabled_ids=tuple(f for f in config if f not in args.disable))
        result = backtest(draws, args.start, args.end, config)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    report = {**asdict(result), 'summary': result.summary, 'collection_status': status,
              'db_sha256_before': fingerprint, 'db_sha256_after': hashlib.sha256(DEFAULT_DB.read_bytes()).hexdigest()}
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    output.with_suffix('.csv').write_bytes(rounds_csv(result.rows, status.get('checked_at', '')))
    output.with_name(output.stem + '_summary.csv').write_bytes(summary_csv(result))
    print(json.dumps({'summary': result.summary, 'elapsed_seconds': result.elapsed_seconds,
                      'db_unchanged': report['db_sha256_before'] == report['db_sha256_after']}, ensure_ascii=True, indent=2))
