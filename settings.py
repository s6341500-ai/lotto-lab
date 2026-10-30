"""버전 있는 필터 설정과 DB와 분리된 JSON 프리셋 저장소."""
from dataclasses import dataclass, asdict, replace
import json
import os
from pathlib import Path
import tempfile

LEGACY_FILTER_IDS = tuple(f'F{i:02d}' for i in range(1, 8))
COMBINATION_FILTER_IDS = tuple(f'F{i:02d}' for i in range(1, 13))
ALL_FILTER_IDS = COMBINATION_FILTER_IDS + ('F13',)
F06_MIN_OCCURRENCES = 3


@dataclass(frozen=True)
class FilterSettings:
    enabled_ids: tuple[str, ...] = ALL_FILTER_IDS
    section_max: int = 2
    f06_min_occurrences: int = F06_MIN_OCCURRENCES
    end_digit_max: int = 2
    max_gap: int = 20
    low_boundary: int = 22
    low_min: int = 2
    low_max: int = 4
    high_min: int = 2
    high_max: int = 4
    similarity_threshold: int = 5
    history_scope: str = 'all'
    history_n: int = 100
    sum_min: int = 100
    sum_max: int = 175
    pool_size: int = 256
    optimization_trials: int = 64

    def __post_init__(self):
        ids = tuple(self.enabled_ids)
        if len(set(ids)) != len(ids) or set(ids) - set(ALL_FILTER_IDS):
            raise ValueError('알 수 없거나 중복된 필터 설정입니다.')
        object.__setattr__(self, 'enabled_ids', tuple(f for f in ALL_FILTER_IDS if f in ids))
        bounds = {'section_max': (2, 6), 'f06_min_occurrences': (2, 5), 'end_digit_max': (1, 6), 'max_gap': (1, 44), 'low_boundary': (1, 44),
                  'low_min': (0, 6), 'low_max': (0, 6), 'high_min': (0, 6), 'high_max': (0, 6),
                  'similarity_threshold': (1, 6), 'history_n': (1, 100000),
                  'sum_min': (21, 255), 'sum_max': (21, 255),
                  'pool_size': (5, 2048), 'optimization_trials': (1, 256)}
        for name, (lo, hi) in bounds.items():
            value = getattr(self, name)
            if type(value) is not int or not lo <= value <= hi:
                raise ValueError(f'{name}: {lo}~{hi} 정수가 필요합니다.')
        if self.history_scope not in ('all', 'recent'):
            raise ValueError('F11 이력 범위는 all 또는 recent여야 합니다.')
        if self.low_min > self.low_max or self.high_min > self.high_max or self.sum_min > self.sum_max:
            raise ValueError('최소값은 최대값보다 클 수 없습니다.')
        if not self.low_min + self.high_min <= 6 <= self.low_max + self.high_max:
            raise ValueError('F10의 저/고번호 개수 범위로 6개를 구성할 수 없습니다.')

    def __iter__(self):
        return iter(self.enabled_ids)

    def __contains__(self, fid):
        return fid in self.enabled_ids

    @property
    def combination_ids(self):
        return tuple(f for f in COMBINATION_FILTER_IDS if f in self)

    def snapshot(self):
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict):
            raise ValueError('설정은 JSON 객체여야 합니다.')
        try:
            return cls(**data)
        except TypeError as exc:
            raise ValueError(f'설정 필드 오류: {exc}') from exc

    @classmethod
    def legacy(cls):
        return cls(enabled_ids=LEGACY_FILTER_IDS)


class SettingsStore:
    def __init__(self, path=Path(__file__).parent / 'data' / 'settings.json'):
        self.path = Path(path)

    def read(self):
        if not self.path.exists():
            return {'version': 1, 'default': asdict(FilterSettings()), 'presets': {}}
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            if data['version'] != 1 or not isinstance(data['presets'], dict):
                raise ValueError('지원하지 않는 설정 파일 형식입니다.')
            FilterSettings.from_dict(data['default'])
            for name, config in data['presets'].items():
                self._name(name)
                FilterSettings.from_dict(config)
            return data
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError(f'설정 파일을 읽을 수 없습니다. 기존 파일을 보존했습니다: {exc}') from exc

    def _write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        task_temp = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent, delete=False) as stream:
                task_temp = stream.name
                json.dump(data, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(task_temp, self.path)
        finally:
            if task_temp and Path(task_temp).exists():
                Path(task_temp).unlink()

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 60:
            raise ValueError('프리셋 이름은 1~60자여야 합니다.')
        return name.strip()

    def default(self):
        return FilterSettings.from_dict(self.read()['default'])

    def save_default(self, settings):
        data = self.read()
        data['default'] = asdict(settings)
        self._write(data)

    def save_preset(self, name, settings):
        data = self.read()
        data['presets'][self._name(name)] = asdict(settings)
        self._write(data)

    def load_preset(self, name):
        data = self.read()
        name = self._name(name)
        if name not in data['presets']:
            raise ValueError('프리셋을 찾을 수 없습니다.')
        return FilterSettings.from_dict(data['presets'][name])

    def delete_preset(self, name, confirmed=False):
        if not confirmed:
            raise ValueError('프리셋 삭제를 먼저 확인해주세요.')
        data = self.read()
        name = self._name(name)
        if name not in data['presets']:
            raise ValueError('프리셋을 찾을 수 없습니다.')
        del data['presets'][name]
        self._write(data)
