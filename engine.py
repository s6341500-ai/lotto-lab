"""전체 조합의 단일 스트리밍 순회, 단계별 집계 및 균등 reservoir 추출."""
import csv
import io
import itertools
import math
import random
import time
import json
from dataclasses import dataclass, replace
from datetime import datetime
from filters import FILTER_IDS, classify_consecutive, f01, f02, f03, f04, f05, f06, f07
from filters import f08, f09, f10, f11, f12, similarity_index, number_mask
from settings import FilterSettings, COMBINATION_FILTER_IDS
from validator import KST
from runtime_support import serialized_analysis

TOTAL_COMBINATIONS = math.comb(45, 6)
SUPPORTED_EXTRACTION_COUNTS = (1, 5, 10)


class InsufficientCandidates(ValueError):
    pass


@dataclass(frozen=True)
class Stage:
    filter_id: str
    enabled: bool
    entered: int
    removed: int
    passed: int


@dataclass(frozen=True)
class AnalysisResult:
    basis_round: int
    enabled: tuple[str, ...]
    seed: int
    total: int
    stages: tuple[Stage, ...]
    group_counts: tuple[tuple[str, int], ...]
    group_samples: tuple[tuple[str, tuple[tuple[int, ...], ...]], ...]
    overall_sample: tuple[tuple[int, ...], ...]
    elapsed_seconds: float
    analyzed_at: str
    settings: FilterSettings | None = None
    candidate_pools: tuple = ()
    f11_index_entries: int = 0
    f11_index_seconds: float = 0.0
    extended_group_samples: tuple = ()
    extended_overall_sample: tuple = ()

    @property
    def remaining(self):
        return sum(dict(self.group_counts).values())


def reservoir_add(sample, combo, seen, size, rng):
    if len(sample) < size:
        sample.append(combo)
    else:
        index = rng.randrange(seen)
        if index < size:
            sample[index] = combo


@serialized_analysis
def analyze(context, enabled=FILTER_IDS, seed=2026, universe=range(1, 46)):
    """후보 전체 대신 기존 5개 표본과 크기를 제한한 F13 탐색 풀만 유지."""
    if set(enabled) - set(COMBINATION_FILTER_IDS):
        raise ValueError('알 수 없는 필터입니다.')
    ids = COMBINATION_FILTER_IDS if context.settings or set(enabled) - set(FILTER_IDS) else FILTER_IDS
    enabled = tuple(f for f in ids if f in enabled)
    config = context.settings or FilterSettings(enabled_ids=enabled)
    if 'F11' in enabled and (context.f11_before is None or 'F11' not in config):
        raise ValueError('F11을 활성화한 과거 이력 문맥을 먼저 생성해야 합니다.')
    required = 10 if 'F06' in enabled else 2 if 'F01' in enabled else 1
    if any(r not in context.recent_rounds for r in range(context.basis_round - required + 1, context.basis_round + 1)):
        raise ValueError(f'데이터 부족: 연속된 최근 {required}회가 필요합니다.')
    universe = tuple(universe)
    if tuple(sorted(set(universe))) != universe or any(type(n) is not int or not 1 <= n <= 45 for n in universe):
        raise ValueError('번호 범위는 오름차순의 중복 없는 1~45 정수여야 합니다.')
    start = time.perf_counter()
    rng = random.Random(seed)
    index_start = time.perf_counter()
    sim_index = similarity_index(context.f11_history, config.similarity_threshold) if 'F11' in enabled else None
    index_seconds = time.perf_counter() - index_start if sim_index else 0.0
    checks = (
        lambda c: f01(c, context.f01_excluded),
        lambda c: f02(c, context.previous), f03, lambda c: f04(c, config.section_max), f05,
        lambda c: f06(c, context.f06_excluded), f07,
        lambda c: f08(c, config.end_digit_max), lambda c: f09(c, config.max_gap),
        lambda c: f10(c, config.low_boundary, config.low_min, config.low_max, config.high_min, config.high_max),
        lambda c: f11(c, sim_index), lambda c: f12(c, config.sum_min, config.sum_max),
    )
    active = [(i, checks[i]) for i, f in enumerate(ids) if f in enabled]
    removed = [0] * len(ids)
    counts = dict(none=0, one_pair=0, multiple=0)
    samples = {key: [] for key in counts}
    overall = []
    pools = {key: [] for key in counts}
    pool_rng = random.Random(seed ^ 0xF13)
    # 기존 5조합 reservoir의 난수열을 보존하면서 10조합용 표본을 별도 수집한다.
    extended_rng = random.Random(seed ^ 0x1010)
    extended_groups = {key: [] for key in counts}
    extended_overall = []
    remaining = 0
    total = 0
    for combo in itertools.combinations(universe, 6):
        total += 1
        for index, check in active:
            if not check(combo):
                removed[index] += 1
                break
        else:
            group = classify_consecutive(combo)
            counts[group] += 1
            remaining += 1
            reservoir_add(samples[group], combo, counts[group], 5, rng)
            reservoir_add(overall, combo, remaining, 5, rng)
            reservoir_add(extended_groups[group], combo, counts[group], 10, extended_rng)
            reservoir_add(extended_overall, combo, remaining, 10, extended_rng)
            if context.settings:
                reservoir_add(pools[group], combo, counts[group], config.pool_size, pool_rng)
    stages = []
    entered = total
    for position, fid in enumerate(ids):
        stages.append(Stage(fid, fid in enabled, entered, removed[position], entered - removed[position]))
        entered -= removed[position]
    return AnalysisResult(context.basis_round, enabled, seed, total, tuple(stages), tuple(counts.items()),
                          tuple((k, tuple(v)) for k, v in samples.items()), tuple(overall),
                          time.perf_counter() - start, datetime.now(KST).isoformat(timespec='seconds'),
                          context.settings, tuple((k, tuple(v)) for k, v in pools.items()) if context.settings else (),
                          len(sim_index.forbidden) if sim_index else 0, index_seconds,
                          tuple((k, tuple(v)) for k, v in extended_groups.items()), tuple(extended_overall))


def generate_numbers(result, optimize=None, count=5):
    """1개는 전체 적격 후보에 균등, 5/10개는 F03 ON일 때 3:2/6:4."""
    if type(count) is not int or count not in SUPPORTED_EXTRACTION_COUNTS:
        raise ValueError('추출 조합 수는 1, 5, 10 중 하나여야 합니다.')
    rng = random.Random(result.seed + 1)
    if 'F03' in result.enabled:
        counts, samples = dict(result.group_counts), dict(result.group_samples)
        if count == 1:
            total = counts['one_pair'] + counts['none']
            if not total:
                raise InsufficientCandidates('후보 부족: 적격 후보가 없습니다. 조건을 자동 완화하지 않습니다.')
            # 그룹을 반반 선택하지 않는다. N그룹/N전체 × 1/N그룹 = 1/N전체.
            group = 'one_pair' if rng.randrange(total) < counts['one_pair'] else 'none'
            selected = (rng.choice(samples[group]),)
        else:
            one, none = (3, 2) if count == 5 else (6, 4)
            if counts['one_pair'] < one or counts['none'] < none:
                raise InsufficientCandidates(f"후보 부족: 연속 1쌍 {counts['one_pair']}개(최소 {one}), 연속 없음 {counts['none']}개(최소 {none}). 조건을 자동 완화하지 않습니다.")
            if count == 10:
                samples = dict(getattr(result, 'extended_group_samples', ()))
            if len(samples.get('one_pair', ())) < one or len(samples.get('none', ())) < none:
                raise InsufficientCandidates('선택한 추출 개수의 표본이 없습니다. 전체 조합 분석을 다시 실행해주세요.')
            selected = rng.sample(samples['one_pair'], one) + rng.sample(samples['none'], none)
            rng.shuffle(selected)
            selected = tuple(selected)
    else:
        if result.remaining < count:
            raise InsufficientCandidates(f'후보 부족: {result.remaining}개(최소 {count}). 조건을 자동 완화하지 않습니다.')
        if count == 1:
            selected = (rng.choice(result.overall_sample),)
        else:
            selected = result.overall_sample if count == 5 else getattr(result, 'extended_overall_sample', ())
        if len(selected) != count:
            raise InsufficientCandidates('선택한 추출 개수의 표본이 없습니다. 전체 조합 분석을 다시 실행해주세요.')
    optimize = ('F13' in result.settings if result.settings else False) if optimize is None else optimize
    return minimize_overlap(result, selected) if optimize and count > 1 else selected


def coverage_stats(selected):
    union = set().union(*map(set, selected)) if selected else set()
    return {'distinct_numbers': len(union), 'total_duplicates': 6 * len(selected) - len(union),
            'shared_by_combo': [sorted(set(combo) & set().union(*(set(c) for j, c in enumerate(selected) if i != j)))
                                for i, combo in enumerate(selected)]}


def minimize_overlap(result, baseline):
    """제한된 reservoir 풀에서 다중 탐욕 시도. 전역 최적을 보장하지 않는다."""
    config = result.settings or FilterSettings()
    count = len(baseline)
    if count == 1:
        return tuple(baseline)
    pools = dict(result.candidate_pools or result.group_samples)
    pools = {g: tuple(dict.fromkeys((*pool, *(c for c in baseline if classify_consecutive(c) == g))))
             for g, pool in pools.items()}
    masks = {c: number_mask(c) for pool in pools.values() for c in pool}
    best, best_score = tuple(baseline), coverage_stats(baseline)['distinct_numbers']
    rng = random.Random(result.seed ^ 0xF1300)
    all_pool = tuple(c for pool in pools.values() for c in pool)
    for _ in range(config.optimization_trials):
        one, none = (3, 2) if count == 5 else (6, 4)
        quotas = ['one_pair'] * one + ['none'] * none if 'F03' in result.enabled else ['any'] * count
        rng.shuffle(quotas)
        chosen, union = [], 0
        for step, group in enumerate(quotas):
            eligible = tuple(c for c in (all_pool if group == 'any' else pools[group]) if c not in chosen)
            if not eligible:
                break
            if step == 0:
                candidate = rng.choice(eligible)
            else:
                gains = [(masks[c] & ~union).bit_count() for c in eligible]
                maximum = max(gains)
                candidate = rng.choice([c for c, gain in zip(eligible, gains) if gain == maximum])
            chosen.append(candidate)
            union |= masks[candidate]
        score = union.bit_count()
        if len(chosen) == count and score > best_score:
            best, best_score = tuple(chosen), score
        if best_score == min(6 * count, 45):
            break
    return best


def results_csv(result, selected, collected_at=''):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['분석 기준 회차', '수집 확인 시각', '분석 시각', '시드', '활성 필터', '조합', '번호1', '번호2', '번호3', '번호4', '번호5', '번호6', '연속쌍 분류', '최종 후보 수', '설정 스냅샷', '서로 다른 번호 수', '총 중복 수', '다른 조합과 겹치는 번호', 'F13 결과 설명', '추출 조합 수', '총 번호 위치 수'])
    stats = coverage_stats(selected)
    for i, combo in enumerate(selected, 1):
        writer.writerow([result.basis_round, collected_at, result.analyzed_at, result.seed, '/'.join(result.enabled), i, *combo, classify_consecutive(combo), result.remaining,
                         (result.settings or FilterSettings(enabled_ids=result.enabled)).snapshot(), stats['distinct_numbers'], stats['total_duplicates'], stats['shared_by_combo'][i - 1],
                         '1조합: F13 적용 제외' if len(selected) == 1 else '중복 최소화 시도 결과 (전역 최적 보장 없음)' if result.settings and 'F13' in result.settings else '무작위 추출',
                         len(selected), len(selected) * 6])
    return out.getvalue().encode('utf-8-sig')


def stages_csv(result):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['필터', '활성', '입력', '제거', '통과', '분석 기준 회차', '설정 스냅샷'])
    for stage in result.stages:
        writer.writerow([stage.filter_id, stage.enabled, stage.entered, stage.removed, stage.passed, result.basis_round,
                         (result.settings or FilterSettings(enabled_ids=result.enabled)).snapshot()])
    return out.getvalue().encode('utf-8-sig')


def backtest(*args, **kwargs):
    from backtesting import backtest as run_backtest
    return run_backtest(*args, **kwargs)


if __name__ == '__main__':
    import argparse
    import json
    from dataclasses import asdict
    from pathlib import Path
    from database import Database
    from filters import build_context, number_availability

    parser = argparse.ArgumentParser(description='저장 DB를 읽어 전체 조합 분석 (수집/DB 수정 없음)')
    parser.add_argument('--basis', type=int, default=None)
    parser.add_argument('--seed', type=int, default=2026)
    parser.add_argument('--count', type=int, choices=SUPPORTED_EXTRACTION_COUNTS, default=5)
    parser.add_argument('--disable', nargs='*', choices=tuple(f'F{i:02d}' for i in range(1, 14)), default=[])
    parser.add_argument('--legacy', action='store_true', help='F01~F07만 ON')
    parser.add_argument('--config', help='FilterSettings JSON 파일')
    parser.add_argument('--output', default='data/analysis_latest.json')
    args = parser.parse_args()
    try:
        from backtesting import read_snapshot
        config = FilterSettings.from_dict(json.loads(Path(args.config).read_text(encoding='utf-8'))) if args.config else (FilterSettings.legacy() if args.legacy else FilterSettings())
        config = replace(config, enabled_ids=tuple(f for f in config if f not in args.disable))
        enabled = config.combination_ids
        draws, collection_status = read_snapshot()
        context = build_context(draws, enabled, args.basis, settings=config)
        result = analyze(context, enabled, args.seed)
    except ValueError as exc:
        parser.exit(1, str(exc) + '\n')
    report = asdict(result)
    report['remaining'] = result.remaining
    report['basis_date'] = context.basis_date
    report['recent_rounds'] = context.recent_rounds
    report['available_numbers'], report['excluded_reasons'] = number_availability(context, enabled)
    report['collection_status'] = collection_status
    selection_error = None
    try:
        selected = generate_numbers(result, count=args.count)
        report['selected'] = selected
        report['extraction_count'] = args.count
    except InsufficientCandidates as exc:
        selection_error = str(exc)
        report['selection_error'] = selection_error
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    output.with_name(output.stem + '_stages.csv').write_bytes(stages_csv(result))
    if not selection_error:
        output.with_name(output.stem + '_picks.csv').write_bytes(results_csv(result, selected, report['collection_status'].get('checked_at', '')))
    else:
        # 같은 출력 경로의 이전 성공 결과를 현재 결과로 오인하지 않도록 정리한다.
        output.with_name(output.stem + '_picks.csv').unlink(missing_ok=True)
    print(json.dumps({'basis': result.basis_round, 'total': result.total, 'remaining': result.remaining,
                      'groups': dict(result.group_counts), 'seconds': result.elapsed_seconds,
                      'selection_error': selection_error, 'report': str(output)}, ensure_ascii=True, indent=2))
    raise SystemExit(1 if selection_error else 0)
