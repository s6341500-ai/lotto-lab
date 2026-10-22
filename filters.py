"""독립 F01~F12 판정 함수. 입력 조합은 오름차순의 서로 다른 6개 번호."""
from collections import Counter
from dataclasses import dataclass
from functools import lru_cache
from itertools import combinations
from settings import FilterSettings, COMBINATION_FILTER_IDS, ALL_FILTER_IDS
from validator import validate_draw

FILTER_IDS = tuple(f'F{i:02d}' for i in range(1, 8))
DESCRIPTIONS = {
    'F01': '최근 2회 모두 나온 번호 제외',
    'F02': '직전 회차와 중복 최대 1개',
    'F03': '연속 0~1쌍 적격 · 추출 5개 3:2 / 10개 6:4',
    'F04': '번호 구간별 최대 2개',
    'F05': '3개 이상 연속 금지',
    'F06': '최근 10회에서 2회 이상 나온 번호 제외',
    'F07': '홀수 개수 2·3·4개 허용',
    'F08': '같은 끝자리 개수 제한',
    'F09': '인접 번호 최대 간격 제한',
    'F10': '저번호/고번호 균형',
    'F11': '과거 당첨 조합 유사도 제한',
    'F12': '번호 합계 범위',
    'F13': '추출 조합 간 중복 최소화 시도',
}


@dataclass(frozen=True)
class FilterContext:
    basis_round: int
    basis_date: str
    recent_rounds: tuple[int, ...]
    previous: frozenset[int]
    f01_excluded: frozenset[int]
    f06_excluded: frozenset[int]
    frequencies: tuple[tuple[int, int], ...]
    settings: FilterSettings | None = None
    f11_history: tuple = ()
    f11_before: int | None = None


def build_context(draws, enabled=FILTER_IDS, basis_round=None, settings=None, history_before=None):
    if isinstance(enabled, FilterSettings):
        settings, enabled = enabled, enabled.combination_ids
    enabled = frozenset(enabled)
    if enabled - set(ALL_FILTER_IDS):
        raise ValueError('알 수 없는 필터입니다.')
    if not draws:
        raise ValueError('분석할 당첨 데이터가 없습니다.')
    basis = basis_round if basis_round is not None else max(d.round for d in draws)
    selected = [validate_draw(d) for d in draws if d.round <= basis]
    by_round = {d.round: d for d in selected}
    if len(by_round) != len(selected):
        raise ValueError('중복 회차 데이터입니다.')
    if basis not in by_round:
        raise ValueError('분석 기준 회차가 저장되어 있지 않습니다.')
    required = 10 if 'F06' in enabled else 2 if 'F01' in enabled else 1
    if basis < required or any(r not in by_round for r in range(basis - required + 1, basis + 1)):
        raise ValueError(f'데이터 부족: 기준 회차까지 연속된 최근 {required}회가 필요합니다.')
    recent = [by_round[r] for r in range(max(1, basis - 9), basis + 1) if r in by_round]
    previous = frozenset(by_round[basis].numbers)
    intersection = previous & frozenset(by_round[basis - 1].numbers) if basis - 1 in by_round else frozenset()
    frequencies = Counter(n for d in recent for n in d.numbers)  # 보너스 제외
    config = settings or (FilterSettings(enabled_ids=tuple(enabled)) if enabled - set(FILTER_IDS) else None)
    before = history_before if history_before is not None else basis
    similarity_history = ()
    if config and 'F11' in enabled:
        similarity_history = select_similarity_history(selected, before, config)
    return FilterContext(basis, by_round[basis].draw_date, tuple(d.round for d in recent),
                         previous, intersection, frozenset(n for n, c in frequencies.items() if c >= 2),
                         tuple(sorted(frequencies.items())), config, similarity_history, before if config else None)


def f01(combo, excluded):
    return excluded.isdisjoint(combo)


def f02(combo, previous):
    return sum(n in previous for n in combo) <= 1


def consecutive_pairs(combo):
    """인접 간격 1의 개수. (1,2,3)은 2쌍으로 계산한다."""
    return sum(b - a == 1 for a, b in zip(combo, combo[1:]))


def classify_consecutive(combo):
    count = consecutive_pairs(combo)
    return 'none' if count == 0 else 'one_pair' if count == 1 else 'multiple'


def f03(combo):
    return consecutive_pairs(combo) <= 1


def f04(combo):
    buckets = [0] * 5
    for n in combo:
        bucket = n // 10  # 1~9 / 10~19 / 20~29 / 30~39 / 40~45
        buckets[bucket] += 1
        if buckets[bucket] > 2:
            return False
    return True


def f05(combo):
    return not any(b == a + 1 and c == b + 1 for a, b, c in zip(combo, combo[1:], combo[2:]))


def f06(combo, excluded):
    return excluded.isdisjoint(combo)


def f07(combo):
    return 2 <= sum(n & 1 for n in combo) <= 4


def f08(combo, maximum=2):
    return max(Counter(n % 10 for n in combo).values()) <= maximum


def f09(combo, maximum=20):
    return max(b - a for a, b in zip(combo, combo[1:])) <= maximum


def f10(combo, boundary=22, low_min=2, low_max=4, high_min=2, high_max=4):
    low = sum(n <= boundary for n in combo)
    return low_min <= low <= low_max and high_min <= 6 - low <= high_max


def number_mask(combo):
    return sum(1 << (n - 1) for n in combo)


def select_similarity_history(draws, before, config):
    """전체/최근 N회는 회차 범위 기준. 누락 이력을 완전한 이력으로 취급하지 않는다."""
    start = max(1, before - config.history_n) if config.history_scope == 'recent' else 1
    history = tuple(sorted((validate_draw(d) for d in draws if start <= d.round < before), key=lambda d: d.round))
    if tuple(d.round for d in history) != tuple(range(start, before)):
        raise ValueError('F11 참조 범위에 누락/중복 회차가 있습니다.')
    return history


@dataclass(frozen=True)
class SimilarityIndex:
    threshold: int
    forbidden: frozenset[int]
    history_masks: tuple[int, ...] = ()


@lru_cache(maxsize=32)
def similarity_index(history, threshold=5, mode='subsets'):
    if type(threshold) is not int or not 1 <= threshold <= 6:
        raise ValueError('F11 중복 임계값은 1~6 정수여야 합니다.')
    if mode == 'masks':
        return SimilarityIndex(threshold, frozenset(), tuple(number_mask(d.numbers) for d in history))
    if mode != 'subsets':
        raise ValueError('지원하지 않는 F11 인덱스 방식입니다.')
    # k개 이상 중복 <=> 공통 k-부분집합이 존재. 기본 k=5이면 회차당 최대 6개.
    return SimilarityIndex(threshold, frozenset(number_mask(part) for d in history
                                              for part in combinations(d.numbers, threshold)))


def f11(combo, index):
    if index.history_masks:
        mask = number_mask(combo)
        return not any((mask & old).bit_count() >= index.threshold for old in index.history_masks)
    bits = tuple(1 << (n - 1) for n in combo)
    return not any(sum(part) in index.forbidden for part in combinations(bits, index.threshold))


def f12(combo, minimum=100, maximum=175):
    return minimum <= sum(combo) <= maximum


def number_availability(context, enabled=FILTER_IDS):
    """단일 번호 제외는 F01/F06만 담당. 나머지는 조합 전체의 제약이다."""
    reasons = {}
    for n in range(1, 46):
        why = []
        if 'F01' in enabled and n in context.f01_excluded:
            why.append('F01: 최근 2회 공통 등장')
        if 'F06' in enabled and n in context.f06_excluded:
            why.append(f'F06: 최근 10회 {dict(context.frequencies)[n]}회 등장')
        if why:
            reasons[n] = tuple(why)
    return tuple(n for n in range(1, 46) if n not in reasons), reasons
