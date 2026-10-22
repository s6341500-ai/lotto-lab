"""판정 재사용 기반 영향도/A-B 비교와 당첨 DB에서 분리한 실험 기록."""
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from functools import lru_cache
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
from uuid import uuid4

import engine
from backtesting import backtest, comparison_context
from settings import FilterSettings, COMBINATION_FILTER_IDS, ALL_FILTER_IDS
from validator import KST


def snapshot(settings):
    return json.loads(settings.snapshot())


def data_fingerprint(draws, end=None):
    # 분석 종료 이후 자료는 판정뿐 아니라 분석 데이터 식별자에서도 제외한다.
    values = [asdict(d) for d in sorted(draws, key=lambda d: d.round) if end is None or d.round <= end]
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class Outcome:
    round: int
    date: str
    numbers: tuple
    verdicts: tuple


@lru_cache(maxsize=8)
def _outcomes(draws, start, end, canonical):
    result = backtest(draws, start, end, canonical)
    return tuple(Outcome(r.round, r.draw_date, r.numbers, tuple(v.passed for v in r.filters)) for r in result.rows)


def outcomes(draws, start, end, settings):
    # 같은 수치 설정에서는 ON/OFF 시나리오마다 과거 번호를 다시 판정하지 않는다.
    canonical = replace(settings, enabled_ids=(), pool_size=256, optimization_trials=64)
    eligible_data = tuple(d for d in draws if d.round <= end)
    return _outcomes(eligible_data, start, end, canonical)


def statuses(rows, settings):
    positions = tuple(i for i, fid in enumerate(COMBINATION_FILTER_IDS) if fid in settings)
    return tuple(None if not r.numbers or any(r.verdicts[i] is None for i in positions)
                 else all(r.verdicts[i] for i in positions) for r in rows)


def aggregate(values):
    possible = sum(v is not None for v in values)
    passed = sum(v is True for v in values)
    return {'requested': len(values), 'evaluable': possible, 'passed': passed,
            'failed': possible - passed, 'unavailable': len(values) - possible,
            'pass_rate': passed / possible if possible else None}


def independent_rates(rows, settings):
    return [{'filter': fid, 'enabled': fid in settings, **aggregate(tuple(r.verdicts[i] for r in rows))}
            for i, fid in enumerate(COMBINATION_FILTER_IDS)]


def off_scenarios(settings):
    return tuple((fid, replace(settings, enabled_ids=tuple(f for f in settings if f != fid)))
                 for fid in COMBINATION_FILTER_IDS if fid in settings)


def _metadata(kind, draws, start, end):
    return {'kind': kind, 'start': start, 'end': end,
            'analyzed_at': datetime.now(KST).isoformat(timespec='seconds'),
            'data_fingerprint': data_fingerprint(draws, end)}


def impact_analysis(draws, start, end, settings):
    begin = time.perf_counter()
    draws = tuple(draws)
    raw = outcomes(draws, start, end, settings)
    base_values = statuses(raw, settings)
    base_stats = aggregate(base_values)
    scenarios, round_rows = [], [{'round': r.round, 'baseline': value} for r, value in zip(raw, base_values)]
    for fid, config in off_scenarios(settings):
        values = statuses(raw, config)
        stats = aggregate(values)
        common = [(a, b) for a, b in zip(base_values, values) if a is not None and b is not None]
        delta = (stats['pass_rate'] - base_stats['pass_rate']) * 100 if stats['pass_rate'] is not None and base_stats['pass_rate'] is not None else None
        common_delta = sum(int(b) - int(a) for a, b in common) / len(common) * 100 if common else None
        scenarios.append({'disabled_filter': fid, 'settings': snapshot(config), **stats,
                          'delta_pp': delta, 'common_evaluable': len(common), 'common_delta_pp': common_delta})
        for row, value in zip(round_rows, values):
            row[f'{fid}_OFF'] = value
    return {**_metadata('impact', draws, start, end), 'settings': snapshot(settings),
            'baseline': base_stats, 'scenarios': scenarios, 'already_off': [f for f in COMBINATION_FILTER_IDS if f not in settings],
            'independent_rates': independent_rates(raw, settings), 'rows': round_rows,
            'elapsed_seconds': time.perf_counter() - begin}


def settings_differences(a, b):
    result = [{'field': fid, 'a': 'ON' if fid in a else 'OFF', 'b': 'ON' if fid in b else 'OFF',
               'role': '추출 전용·통과율 제외' if fid == 'F13' else '조합 조건'}
              for fid in ALL_FILTER_IDS if (fid in a) != (fid in b)]
    for key, value in asdict(a).items():
        if key != 'enabled_ids' and value != getattr(b, key):
            result.append({'field': key, 'a': value, 'b': getattr(b, key),
                           'role': '추출 전용·통과율 제외' if key in ('pool_size', 'optimization_trials') else '조건 수치'})
    return result


def ab_analysis(draws, start, end, a, b):
    begin = time.perf_counter()
    draws = tuple(draws)
    raw_a, raw_b = outcomes(draws, start, end, a), outcomes(draws, start, end, b)
    va, vb = statuses(raw_a, a), statuses(raw_b, b)
    groups = {key: [] for key in ('a_only', 'b_only', 'both', 'neither', 'unavailable_either',
                                  'a_pass_b_unavailable', 'b_pass_a_unavailable')}
    records = []
    for ra, rb, av, bv in zip(raw_a, raw_b, va, vb):
        if av is None or bv is None:
            group = 'unavailable_either'
            if av is True:
                groups['a_pass_b_unavailable'].append(ra.round)
            if bv is True:
                groups['b_pass_a_unavailable'].append(ra.round)
        else:
            group = 'both' if av and bv else 'a_only' if av else 'b_only' if bv else 'neither'
        groups[group].append(ra.round)
        records.append({'round': ra.round, 'date': ra.date, 'numbers': list(ra.numbers),
                        'a': av, 'b': bv, 'group': group,
                        'failed_a': [f for f, v in zip(COMBINATION_FILTER_IDS, ra.verdicts) if f in a and v is False],
                        'failed_b': [f for f, v in zip(COMBINATION_FILTER_IDS, rb.verdicts) if f in b and v is False]})
    return {**_metadata('ab', draws, start, end), 'settings_a': snapshot(a), 'settings_b': snapshot(b),
            'a': aggregate(va), 'b': aggregate(vb),
            'independent_a': independent_rates(raw_a, a), 'independent_b': independent_rates(raw_b, b),
            'overlap': {**{key: len(value) for key, value in groups.items()},
                        'common_evaluable': sum(len(groups[g]) for g in ('a_only', 'b_only', 'both', 'neither'))},
            'round_groups': groups, 'rows': records, 'differences': settings_differences(a, b),
            'elapsed_seconds': time.perf_counter() - begin}


def precise_ab(draws, target_round, a, b, analyzer=None):
    """대상 회차 직전 문맥으로만 전체 조합을 비교한다. 기본 분석에서는 호출하지 않는다."""
    begin = time.perf_counter()
    contexts = [comparison_context(draws, target_round, config) for config in (a, b)]
    analyzer = analyzer or engine.analyze
    results, computed = [], {}
    for ctx, config in zip(contexts, (a, b)):
        key = (ctx, config.combination_ids)
        if key not in computed:
            computed[key] = analyzer(ctx, config.combination_ids, 2026)
        results.append(computed[key])
    return {'kind': 'precision_ab', 'target_round': target_round, 'basis_round': target_round - 1,
            'settings_a': snapshot(a), 'settings_b': snapshot(b),
            'data_fingerprint': data_fingerprint(draws, target_round - 1),
            'analyzed_at': datetime.now(KST).isoformat(timespec='seconds'),
            'computed_profiles': len(computed),
            **{label: {'total': r.total, 'candidates': r.remaining, 'candidate_rate': r.remaining / r.total,
                       'groups': dict(r.group_counts), 'elapsed_seconds': r.elapsed_seconds,
                       'basis_round': r.basis_round} for label, r in zip(('a', 'b'), results)},
            'elapsed_seconds': time.perf_counter() - begin}


def impact_records(result):
    base = {'시나리오': '기준 설정', **result['baseline'], 'delta_pp': 0.0, 'settings': result['settings']}
    records = [base] + [{'시나리오': f"{s['disabled_filter']} OFF", **s} for s in result['scenarios']]
    return [{'시나리오': r['시나리오'], '통과': r['passed'], '실패': r['failed'], '분석 회차': r['evaluable'],
             '분석 불가': r['unavailable'], '통과율(%)': r['pass_rate'] * 100 if r['pass_rate'] is not None else None,
             '차이(%p)': r['delta_pp'], '공통 판정 회차': r.get('common_evaluable', r['evaluable']),
             '공통 회차 차이(%p)': r.get('common_delta_pp', 0.0), '설정 스냅샷': json.dumps(r['settings'], ensure_ascii=False)}
            for r in records]


def summary_records(result):
    if result['kind'] == 'impact':
        return impact_records(result)
    return [{'설정': label.upper(), '통과': result[label]['passed'], '실패': result[label]['failed'],
             '분석 회차': result[label]['evaluable'], '분석 불가': result[label]['unavailable'],
             '통과율(%)': result[label]['pass_rate'] * 100 if result[label]['pass_rate'] is not None else None,
             '설정 스냅샷': json.dumps(result['settings_' + label], ensure_ascii=False)} for label in ('a', 'b')]


def analysis_csv(result, detail=False):
    records = result['rows'] if detail else summary_records(result)
    metadata = {'분석 시작': result['start'], '분석 종료': result['end'], '분석 시각': result['analyzed_at'],
                '데이터 식별자': result['data_fingerprint'],
                '전체 설정': json.dumps({k: v for k, v in result.items() if k.startswith('settings')}, ensure_ascii=False)}
    return records_csv([{**metadata, **row} for row in records])


def records_csv(records):
    out = io.StringIO(newline='')
    if records:
        fields = tuple(dict.fromkeys(key for row in records for key in row))
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows({k: json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, tuple)) else v for k, v in row.items()} for row in records)
    return out.getvalue().encode('utf-8-sig')


class ExperimentStore:
    """설정 프리셋과 별개인 JSON 저장소. 삭제는 매번 명시적 확인이 필요하다."""
    def __init__(self, path=Path(__file__).parent / 'data' / 'experiments.json'):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {'version': 1, 'experiments': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['version'] != 1 or not isinstance(data['experiments'], dict):
                raise ValueError('실험 파일 형식 오류')
            for key, record in data['experiments'].items():
                if record['id'] != key or not record['name'].strip():
                    raise ValueError('실험 식별자/이름 오류')
                self._validate_result(record['result'])
                if record.get('precision') is not None:
                    self._validate_precision(record['precision'], record['result'])
            return data
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f'실험 기록을 읽을 수 없습니다. 기존 파일을 보존했습니다: {exc}') from exc

    @staticmethod
    def _validate_result(result):
        if result['kind'] not in ('impact', 'ab') or not 1 <= result['start'] <= result['end']:
            raise ValueError('지원하지 않는 실험 결과/기간입니다.')
        for key in ('settings',) if result['kind'] == 'impact' else ('settings_a', 'settings_b'):
            if set(result[key]) != set(asdict(FilterSettings())):
                raise ValueError('전체 설정 스냅샷이 필요합니다.')
            FilterSettings.from_dict(result[key])
        if not isinstance(result['rows'], list) or not result['analyzed_at'] or not result['data_fingerprint']:
            raise ValueError('실험 결과가 불완전합니다.')
        summary_records(result)

    @staticmethod
    def _validate_precision(precision, result):
        if result['kind'] != 'ab' or precision['kind'] != 'precision_ab':
            raise ValueError('A/B 정밀 분석 형식이 아닙니다.')
        if any(precision[k] != result[k] for k in ('settings_a', 'settings_b')):
            raise ValueError('정밀 분석과 A/B 설정이 일치하지 않습니다.')
        if type(precision['target_round']) is not int or precision['target_round'] < 2 or precision['basis_round'] != precision['target_round'] - 1:
            raise ValueError('정밀 분석의 대상/기준 회차가 올바르지 않습니다.')
        for label in ('a', 'b'):
            stat = precision[label]
            if (stat['basis_round'] != precision['basis_round'] or type(stat['total']) is not int
                    or stat['total'] <= 0 or type(stat['candidates']) is not int
                    or not 0 <= stat['candidates'] <= stat['total']):
                raise ValueError('정밀 분석 후보 수 또는 기준 회차 오류입니다.')

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
                temporary = stream.name
                json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary and Path(temporary).exists():
                Path(temporary).unlink()

    def save(self, name, result, precision=None):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 100:
            raise ValueError('실험 이름은 1~100자여야 합니다.')
        frozen = json.loads(json.dumps(result, ensure_ascii=False, allow_nan=False))
        try:
            self._validate_result(frozen)
            if precision is not None:
                self._validate_precision(precision, frozen)
        except (KeyError, TypeError, AttributeError) as exc:
            raise ValueError('저장할 실험 결과가 불완전합니다.') from exc
        data = self.read()
        key = uuid4().hex
        record = {'id': key, 'name': name.strip(), 'saved_at': datetime.now(KST).isoformat(timespec='seconds'),
                  'result': frozen, 'precision': json.loads(json.dumps(precision)) if precision else None}
        data['experiments'][key] = record
        self._write(data)
        return record

    def load(self, key):
        try:
            return self.read()['experiments'][key]
        except KeyError as exc:
            raise ValueError('선택한 실험 기록이 없습니다.') from exc

    def delete(self, key, confirmed=False):
        if confirmed is not True:
            raise ValueError('선택한 실험의 삭제를 먼저 확인해주세요.')
        data = self.read()
        if key not in data['experiments']:
            raise ValueError('선택한 실험 기록이 없습니다.')
        del data['experiments'][key]
        self._write(data)


def experiment_csv(record):
    metadata = {'실험 ID': record['id'], '실험 이름': record['name'], '저장 시각': record['saved_at'],
                '분석 시작': record['result']['start'], '분석 종료': record['result']['end'],
                '분석 시각': record['result']['analyzed_at'], '데이터 식별자': record['result']['data_fingerprint'],
                '정밀 분석': record.get('precision')}
    return records_csv([{**metadata, **row} for row in summary_records(record['result'])])
