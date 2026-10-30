import json
import importlib
import secrets
import os
from dataclasses import asdict, replace
import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx
from collector import Collector
from database import Database
from filters import FILTER_IDS, DESCRIPTIONS, build_context, number_availability, classify_consecutive
# Streamlit의 장기 실행 프로세스에 4단계 이전 engine이 남아 있으면
# 최신 app만 재실행될 때 새 export를 찾지 못한다. 기존 구현을 한 번 다시 읽는다.
import engine as _engine
if not all(hasattr(_engine, name) for name in (
        'analyze', 'generate_numbers', 'results_csv', 'stages_csv',
        'InsufficientCandidates', 'coverage_stats', 'SUPPORTED_EXTRACTION_COUNTS')):
    importlib.invalidate_caches()
    importlib.reload(_engine)
from engine import analyze, generate_numbers, results_csv, stages_csv, InsufficientCandidates, coverage_stats
from engine import SUPPORTED_EXTRACTION_COUNTS
from settings import FilterSettings, SettingsStore, COMBINATION_FILTER_IDS, ALL_FILTER_IDS
from backtesting import (validate_round, backtest, comparison_context, read_snapshot,
                         round_records, rounds_csv, summary_csv, PATTERN_LABELS,
                         independent_filter_rates, rate_indicator)

st.set_page_config(page_title='Lotto Lab', page_icon='🎱', layout='wide')
st.markdown('''<style>.stApp {background:#f6f8fc} div[data-testid="stMetric"] {
background:white; padding:20px; border-radius:16px; border:1px solid #e5eaf3}
.ball {display:inline-flex;width:48px;height:48px;border-radius:50%;align-items:center;
justify-content:center;color:white;font-weight:700;font-size:20px;margin:5px;background:#4169b1}</style>''', unsafe_allow_html=True)
st.markdown('''<style>
.pick-card {background:white;border:1px solid #dfe5ef;border-radius:14px;padding:12px;margin:12px 0}
.pick-balls {display:flex;gap:7px;flex-wrap:nowrap;margin:8px 0}
.pick-balls .ball {margin:0;width:42px;height:42px;flex:0 0 42px}
.pick-detail {font-size:14px;color:#42516a;overflow-wrap:anywhere}
.bonus-group {display:inline-flex;align-items:center;white-space:nowrap}
@media (max-width:640px) {
  h1 {font-size:1.85rem !important;line-height:1.3 !important;word-break:keep-all}
  .stMainBlockContainer {padding:3.8rem 1rem 2rem}
  div[data-testid="stMetric"] {padding:12px}
  .stMain button[data-testid^="stBaseButton"], [data-testid="stSidebar"] button[data-testid^="stBaseButton"],
  [data-testid="stCheckbox"] label, [data-testid="stRadio"] label {min-height:44px !important}
  button[aria-label="Expand sidebar"], button[aria-label="Collapse sidebar"] {min-height:44px !important;min-width:44px !important}
  [data-testid="stRadio"] [role="radiogroup"] {gap:8px}
  .ball {width:36px;height:36px;font-size:18px;margin:3px}
  .pick-balls {gap:5px}
  .pick-balls .ball {width:clamp(30px,9vw,42px);height:clamp(30px,9vw,42px);flex-basis:clamp(30px,9vw,42px)}
  .pick-card {padding:10px}
}
</style>''', unsafe_allow_html=True)
db = Database()
st.sidebar.title('🎱 Lotto Lab')
st.sidebar.caption('개인용 로또 6/45 분석실')
menu = st.sidebar.radio('메뉴', ['대시보드', '조건 설정', '번호 추출', '당첨번호 검증', '필터 분석'])
st.sidebar.caption('5단계 · 필터 영향도 및 조건 조합 비교')
settings_store = SettingsStore(db.path.parent / 'settings.json')
with st.sidebar.expander('개인 데이터 백업 / 복원'):
    st.caption('클라우드 로컬 저장소는 영구 보존되지 않습니다. 변경한 프리셋·실험 기록을 파일로 내려받아 보관하세요. 접속자끼리 저장소를 공유하므로 개인용 비공개 앱을 권장합니다.')
    from personal_backup import export_personal, restore_personal
    try:
        st.download_button('개인 설정·실험 백업', export_personal(db.path.parent),
                           'lotto_lab_personal_backup.json', 'application/json')
    except ValueError as exc:
        st.error(str(exc))
    uploaded = st.file_uploader('개인 백업 JSON 복원', type=['json'])
    if uploaded is not None:
        confirmed = st.checkbox('현재 개인 설정·실험 기록을 덮어씁니다')
        if st.button('백업 복원', disabled=not confirmed):
            try:
                restore_personal(db.path.parent, uploaded.getvalue(), confirmed=confirmed)
                st.success('복원했습니다. 설정을 적용하려면 브라우저를 새로고침하세요.')
            except (ValueError, OSError) as exc:
                st.error(str(exc))
if 'numeric_config' not in st.session_state:
    try:
        initial = settings_store.default()
    except ValueError as exc:
        st.error(str(exc))
        initial = FilterSettings()
    old_flags = st.session_state.get('filter_config', {})
    st.session_state.filter_config = {f: old_flags.get(f, f in initial) for f in ALL_FILTER_IDS}
    st.session_state.numeric_config = {k: v for k, v in asdict(initial).items() if k != 'enabled_ids'}
st.session_state.numeric_config.setdefault('section_max', 2)


def current_settings():
    return FilterSettings(enabled_ids=tuple(f for f in ALL_FILTER_IDS if st.session_state.filter_config[f]),
                          **st.session_state.numeric_config)


@st.cache_data(show_spinner=False, max_entries=16)
def cached_independent_rates(draws, start, end, canonical):
    return independent_filter_rates(draws, start, end, canonical)


def render_backtest_period(draws):
    latest = draws[-1].round
    saved = st.session_state.get('selected_backtest_range', (min(11, latest), latest))
    saved = tuple(max(1, min(value, latest)) for value in saved)
    st.session_state.selected_backtest_range = saved

    def change_period(index):
        values = list(st.session_state.selected_backtest_range)
        values[index] = st.session_state[f'backtest_period_{index}']
        st.session_state.selected_backtest_range = tuple(values)

    with st.expander('독립 통과율 백테스트 기간', expanded=True):
        start = int(st.number_input('검증 시작 회차', min_value=1, max_value=latest, value=saved[0],
                                    step=1, key='backtest_period_0', on_change=change_period, args=(0,)))
        end = int(st.number_input('검증 종료 회차', min_value=1, max_value=latest, value=saved[1],
                                  step=1, key='backtest_period_1', on_change=change_period, args=(1,)))
        st.caption(f'조건별 독립 통과율 / 백테스트 기간: {start}~{end}회 · 판정 가능한 실제 당첨번호만 분모에 포함 (보너스 제외)')
        st.caption('과거 통과율은 당첨 확률이나 미래 예측력을 의미하지 않습니다. 과거 데이터로 선택한 기준값의 성능을 미래 성능으로 단정할 수 없습니다.')
        if start > end:
            st.error('시작 회차는 종료 회차보다 클 수 없습니다. 조건별 통과율은 분석 불가입니다.')
    return start, end


def render_controls(prefix, draws, start, end, extraction_count=5):
    def change_flag(fid):
        st.session_state.filter_config[fid] = st.session_state[prefix + fid]

    def change_value(name):
        st.session_state.numeric_config[name] = st.session_state[prefix + 'param_' + name]

    rate_slots = {}
    st.selectbox('F04 구간별 최대 허용 개수', [2, 3, 4, 5, 6],
                 index=st.session_state.numeric_config['section_max'] - 2,
                 format_func=lambda n: '6개 (제한 없음)' if n == 6 else f'{n}개',
                 key=prefix + 'param_section_max', on_change=change_value, args=('section_max',))
    for fid in COMBINATION_FILTER_IDS:
        control, rate = st.columns([3, 4])
        with control:
            st.checkbox(f'{fid} · {DESCRIPTIONS[fid]}', value=st.session_state.filter_config[fid],
                        key=prefix + fid, on_change=change_flag, args=(fid,))
        with rate:
            rate_slots[fid] = st.empty()
    st.markdown('**추출 최적화 (개별 조합 판정 제외)**')
    st.checkbox('F13 · 추출 조합 간 중복 최소화 시도', value=st.session_state.filter_config['F13'],
                key=prefix + 'F13', on_change=change_flag, args=('F13',), disabled=extraction_count == 1)
    if extraction_count == 1:
        st.caption('1조합에서는 F13을 적용하지 않습니다. 저장된 ON/OFF 설정은 유지합니다.')
    with st.expander('F08~F12 수치 및 F13 시도 범위 편집'):
        fields = [('end_digit_max', 'F08 같은 끝자리 최대 개수', 1, 6),
                  ('max_gap', 'F09 인접 최대 간격', 1, 44), ('low_boundary', 'F10 저번호 상한', 1, 44),
                  ('low_min', 'F10 저번호 최소 개수', 0, 6), ('low_max', 'F10 저번호 최대 개수', 0, 6),
                  ('high_min', 'F10 고번호 최소 개수', 0, 6), ('high_max', 'F10 고번호 최대 개수', 0, 6),
                  ('similarity_threshold', 'F11 제외 중복 개수 이상', 1, 6),
                  ('history_n', 'F11 최근 N회', 1, 100000),
                  ('sum_min', 'F12 합계 최소', 21, 255), ('sum_max', 'F12 합계 최대', 21, 255),
                  ('pool_size', 'F13 그룹별 후보 풀 상한', 5, 2048),
                  ('optimization_trials', 'F13 중복 최소화 시도 횟수', 1, 256)]
        for name, label, low, high in fields:
            st.number_input(label, min_value=low, max_value=high, value=st.session_state.numeric_config[name],
                            step=1, key=prefix + 'param_' + name, on_change=change_value, args=(name,))
        st.selectbox('F11 과거 참조 범위', ['all', 'recent'],
                     index=0 if st.session_state.numeric_config['history_scope'] == 'all' else 1,
                     format_func=lambda s: '전체 과거 회차' if s == 'all' else '최근 N회차',
                     key=prefix + 'param_history_scope', on_change=change_value, args=('history_scope',))
    try:
        config = current_settings()
    except ValueError as exc:
        for fid, slot in rate_slots.items():
            slot.markdown(rate_indicator(None, st.session_state.filter_config[fid])['label'])
        st.error('설정 오류: ' + str(exc))
        render_presets()
        st.stop()
    stats = {}
    if start <= end:
        canonical = replace(config, enabled_ids=(), pool_size=256, optimization_trials=64)
        try:
            with st.spinner('실제 당첨번호 독립 통과율 계산 중…'):
                stats = {s['filter']: s for s in cached_independent_rates(tuple(draws), start, end, canonical)}
        except ValueError as exc:
            st.error('조건별 통과율 분석 불가: ' + str(exc))
    for fid, slot in rate_slots.items():
        indicator = rate_indicator(stats.get(fid), fid in config)
        slot.markdown(indicator['label'], help=indicator['help'])
    with st.expander('현재 적용 설정 스냅샷'):
        st.json(asdict(config))
    return config


def apply_config(config):
    st.session_state.filter_config = {f: f in config for f in ALL_FILTER_IDS}
    st.session_state.numeric_config = {k: v for k, v in asdict(config).items() if k != 'enabled_ids'}
    for key in list(st.session_state):
        if key.startswith(('toggle_', 'validation_toggle_')):
            del st.session_state[key]


def render_presets():
    def action(kind):
        try:
            if kind == 'save_default':
                settings_store.save_default(current_settings())
            elif kind == 'restore_default':
                apply_config(settings_store.default())
            elif kind == 'factory':
                apply_config(FilterSettings())
            elif kind == 'legacy':
                apply_config(FilterSettings.legacy())
            elif kind == 'save':
                settings_store.save_preset(st.session_state.preset_name, current_settings())
                st.session_state.pop('confirm_delete_' + st.session_state.preset_name.strip(), None)
            elif kind == 'load':
                apply_config(settings_store.load_preset(st.session_state.selected_preset))
            elif kind == 'delete':
                name = st.session_state.selected_preset
                settings_store.delete_preset(name, st.session_state.get('confirm_delete_' + name, False))
                st.session_state.pop('selected_preset', None)
                st.session_state.pop('confirm_delete_' + name, None)
            st.session_state.preset_notice = '설정을 적용했습니다.'
        except (ValueError, OSError) as exc:
            st.session_state.preset_notice = str(exc)

    with st.expander('기본값 및 사용자 프리셋 저장/복원', expanded=False):
        if st.session_state.get('preset_notice'):
            st.info(st.session_state.preset_notice)
        st.button('현재 설정을 기본값으로 저장', on_click=action, args=('save_default',))
        st.button('저장 기본값 복원', on_click=action, args=('restore_default',))
        st.button('공장 기본값 복원 (F01~F13 ON)', on_click=action, args=('factory',))
        st.button('2단계 재현 설정 (F08~F13 OFF)', on_click=action, args=('legacy',))
        st.text_input('저장할 프리셋 이름 (동일 이름은 덮어씀)', key='preset_name')
        st.button('사용자 프리셋 저장', on_click=action, args=('save',))
        try:
            names = sorted(settings_store.read()['presets'])
        except ValueError as exc:
            st.error(str(exc))
            names = []
        if names:
            st.selectbox('사용자 프리셋', names, key='selected_preset')
            st.button('선택 프리셋 불러오기', on_click=action, args=('load',))
            name = st.session_state.selected_preset
            confirmed = st.checkbox(f"'{name}' 프리셋 삭제 확인", key='confirm_delete_' + name)
            st.button('선택 프리셋 삭제', disabled=not confirmed, on_click=action, args=('delete',))


@st.cache_data(show_spinner=False, max_entries=16)
def cached_analysis(context, enabled, seed):
    """1/5/10조합 표본을 지원하는 분석 캐시. 추출 개수는 후보 조건이 아니다."""
    # F13 ON/OFF와 시도 횟수 변경은 후보 재순회를 유발하지 않는다.
    canonical = context
    if context.settings:
        config = replace(context.settings, enabled_ids=tuple(f for f in context.settings if f != 'F13'), optimization_trials=64)
        canonical = replace(context, settings=config)
    result = cached_candidates(canonical, enabled, seed)
    return replace(result, settings=context.settings)


@st.cache_data(show_spinner=False, max_entries=16)
def cached_candidates(context, enabled, seed):
    """1/5/10조합 표본을 포함한 분석 캐시. 추출 개수 변경은 재순회하지 않는다."""
    return analyze(context, enabled, seed)


@st.cache_data(show_spinner=False, max_entries=16)
def cached_backtest(draws, start, end, enabled):
    canonical = enabled
    if isinstance(enabled, FilterSettings):
        canonical = replace(enabled, enabled_ids=tuple(f for f in enabled if f != 'F13'), pool_size=256, optimization_trials=64)
    result = cached_actual_backtest(draws, start, end, canonical)
    return replace(result, settings=enabled, rows=tuple(replace(row, settings=enabled) for row in result.rows)) if isinstance(enabled, FilterSettings) else result


@st.cache_data(show_spinner=False, max_entries=16)
def cached_actual_backtest(draws, start, end, enabled):
    return backtest(draws, start, end, enabled)


def render_verdict(result):
    st.subheader(f'{result.round}회 실제 당첨번호')
    if result.numbers:
        st.write(' · '.join(map(str, result.numbers)))
        st.caption(f'추첨일 {result.draw_date} · 보너스 제외 · 연속번호 {PATTERN_LABELS[result.consecutive_pattern]} ({result.pair_count}쌍)')
    st.caption(f'참조 이력: {result.history_rounds[0] if result.history_rounds else "없음"}~{result.round - 1}회 ({len(result.history_rounds)}회) · 대상/미래 회차 제외')
    if result.overall_passed is None:
        st.warning('분석 불가: ' + result.unavailable_reason)
    elif result.overall_passed:
        st.success('활성화된 필터 전체 통과' if any(v.enabled for v in result.filters) else '활성 필터 없음: 조건 미적용 통과')
    else:
        st.error('활성화된 필터 실패: ' + ', '.join(result.failed_filters))
    st.dataframe([{'조건': v.filter_id, '적용': 'ON' if v.enabled else 'OFF',
                   '독립 판정': '분석 불가' if v.passed is None else '통과' if v.passed else '실패',
                   '판정 근거': v.reason} for v in result.filters], hide_index=True, use_container_width=True)
    st.caption('OFF 조건의 독립 판정은 참고용이며 전체 통과 여부에 반영하지 않습니다.')
    st.caption('F13: 추출 최적화 전용. 실제 당첨번호의 통과/실패 및 통과율 대상이 아닙니다.')
    with st.expander('활성 조건의 제외 번호와 실제 당첨번호 중 해당 번호'):
        if result.excluded_numbers:
            st.dataframe([{'제외 번호': n, '실제 당첨번호에 포함': n in result.numbers, '제외 사유': ' / '.join(why)}
                          for n, why in result.excluded_numbers], hide_index=True)
        else:
            st.write('계산 가능한 활성 조건에서 단일 번호 제외 없음')


def render_precision(draws, target_round, enabled, key):
    st.subheader('선택 회차 정밀 비교 (선택 사항)')
    st.caption('선택 회차 직전 데이터로 전체 8,145,060개 조합의 통과율을 계산합니다. 범위 전체를 순회하는 분석은 아닙니다.')
    try:
        context = comparison_context(draws, target_round, enabled)
    except ValueError as exc:
        st.info('정밀 비교 분석 불가: ' + str(exc))
        return
    signature = (target_round, context, enabled)
    if st.button('선택 회차 전체 조합 정밀 분석', key=key + '_button'):
        with st.spinner('선택 회차 직전 기준으로 전체 조합을 분석 중…'):
            result = cached_analysis(context, enabled.combination_ids if isinstance(enabled, FilterSettings) else enabled, 2026)
        st.session_state[key] = (signature, result)
    saved = st.session_state.get(key)
    if saved and saved[0] == signature:
        result = saved[1]
        st.metric(f'{target_round}회 직전 기준 전체 조합 통과율', f'{result.remaining / result.total:.4%}')
        st.write(f'{result.remaining:,} / {result.total:,}개 · 기준 {context.basis_round}회까지 · 계산 {result.elapsed_seconds:.2f}초')
        st.caption('기본 백테스트는 실제 당첨번호의 조건 적격 여부를 측정합니다. 이 값은 선택 회차의 후보 공간 비율이며 전체 기간의 평균 비율이 아닙니다. F03의 5조합 구성 비율은 단일 조합 통과율에 포함하지 않습니다.')
        st.download_button('정밀 비교 단계 CSV 저장', stages_csv(result), f'lotto_comparison_before_{target_round}.csv', 'text/csv', key=key + '_csv')
    elif saved:
        st.caption('회차 또는 조건이 변경되었습니다. 정밀 분석 버튼으로 다시 계산하세요.')


def update():
    with st.spinner('동행복권 최신 회차 확인 및 누락 데이터 수집 중…'):
        return Collector().update(db)


# 세션 최초 실행 시 확인. UI 조작에 따른 rerun마다 요청하지 않는다.
if (get_script_run_ctx(suppress_warning=True) is not None
        and menu == '대시보드' and 'startup_checked' not in st.session_state
        and os.environ.get('LOTTO_LAB_AUTO_UPDATE', '1') != '0'):
    st.session_state.startup_checked = True
    update()

if menu == '대시보드':
    st.title('로또 데이터 대시보드')
    st.caption('역대 당첨번호를 검증하고 안전하게 보관합니다.')
    if db.get_meta('seed_source'):
        st.caption(db.get_meta('seed_source'))
    st.caption('데이터 출처: ' + (db.get_meta('data_source') or '배포에 포함된 검증된 동행복권 스냅샷'))
    st.caption('마지막 수집 성공: ' + (db.get_meta('last_success') or '기록 없음 · 초기 데이터로 분석 가능'))
    st.caption('재배포 시 버전 관리된 seed 스냅샷에서 복원합니다. 실행 중 SQLite 변경은 GitHub에 자동 반영되지 않습니다.')
    if st.button('↻ 수동 새로고침', type='primary'):
        update()
    status = json.loads(db.get_meta('update_status') or '{}')
    if status:
        (st.success if status.get('ok') else st.error)(status['message'])
        st.caption(f"최근 확인: {status['checked_at']} (한국 시간)")
    draws = db.all()
    latest = draws[-1] if draws else None
    a, b, c = st.columns(3)
    a.metric('최신 저장 회차', f'{latest.round}회' if latest else '없음')
    b.metric('저장된 회차 수', f'{len(draws):,}회')
    missing = db.missing(latest.round) if latest else []
    c.metric('누락 회차 수', len(missing))
    if latest:
        st.subheader(f'{latest.round}회 당첨번호')
        st.caption(f'추첨일 {latest.draw_date}')
        balls = ''.join(f'<span class="ball">{n}</span>' for n in latest.numbers)
        st.markdown(balls + f'<span class="bonus-group"> + 보너스 <span class="ball" style="background:#7958ad">{latest.bonus}</span></span>', unsafe_allow_html=True)
        if missing:
            st.warning('누락 회차: ' + ', '.join(map(str, missing)))
        st.subheader('저장된 당첨 이력')
        st.dataframe([{'회차': d.round, '추첨일': d.draw_date, '당첨번호': ' · '.join(map(str, d.numbers)), '보너스': d.bonus} for d in reversed(draws)], hide_index=True, use_container_width=True)
    else:
        st.info('저장된 데이터가 없습니다. 연결 상태를 확인하고 수동 새로고침을 눌러주세요.')
elif menu in ('조건 설정', '번호 추출'):
    st.title(menu)
    st.caption('보너스 번호를 제외한 당첨번호 6개만 분석합니다.')
    draws = db.all()
    status = json.loads(db.get_meta('update_status') or '{}')
    collected_at = status.get('checked_at', '기록 없음')
    st.caption(f'최근 수집 확인 시점: {collected_at} · 실제 저장 회차 {len(draws):,}회')
    if status and not status.get('ok'):
        st.warning('최근 수집 확인 실패: 저장된 데이터 기준으로 분석합니다. ' + status['message'])
    if not draws:
        st.error('분석할 데이터가 없습니다. 대시보드에서 수집을 먼저 실행하세요.')
    else:
        st.subheader('적용 조건')
        if 'selected_extraction_count' not in st.session_state:
            st.session_state.selected_extraction_count = 5

        def save_extraction_count():
            st.session_state.selected_extraction_count = st.session_state.extraction_count_widget

        count = st.radio('추출 조합 수', SUPPORTED_EXTRACTION_COUNTS,
                         index=SUPPORTED_EXTRACTION_COUNTS.index(st.session_state.selected_extraction_count),
                         format_func=lambda n: f'{n}조합', horizontal=True,
                         key='extraction_count_widget', on_change=save_extraction_count)
        run_slot = st.empty()
        with st.expander('조건 ON/OFF 및 수치 설정', expanded=menu == '조건 설정'):
            start, end = render_backtest_period(draws)
            config = render_controls('toggle_', draws, start, end, count)
        st.caption(f'조건별 과거 독립 통과율 기준 기간: {start}~{end}회')
        enabled = config.combination_ids
        st.caption('고정 처리 순서: F01 → … → F12. 제거 수는 앞 단계 통과 조합에 대한 순서 의존적인 값입니다. F13은 최종 추출에만 적용합니다.')
        st.caption('F03 ON: 1조합은 두 그룹의 실제 후보 수에 비례한 균등 추출, 5조합은 1쌍 3개+없음 2개, 10조합은 1쌍 6개+없음 4개. F03 OFF는 유형 할당량 없이 추출하며 F05는 별도 적용합니다.')
        basis = st.selectbox('분석 기준 회차', [d.round for d in reversed(draws)], format_func=lambda r: f'{r}회')
        if 'analysis_seed' not in st.session_state:
            st.session_state.analysis_seed = secrets.randbelow(2 ** 31)
        seed = int(st.number_input('무작위 시드', min_value=0, max_value=2 ** 31 - 1, step=1, key='analysis_seed'))
        st.caption('같은 데이터·조건·시드는 같은 결과를 재사용합니다. 새 추출은 시드를 바꾸고 실행하세요.')
        try:
            context = build_context(draws, enabled, basis, settings=config)
        except ValueError as exc:
            st.error(str(exc))
        else:
            st.write(f'분석 기준: {basis}회 ({context.basis_date}) · 최근 10회 창: {max(1, basis - 9)}~{basis}회')
            st.caption(f'F11 참조: 분석 기준 {basis}회 미포함, 이전 {len(context.f11_history)}회 ({config.history_scope}). 검증/백테스트에서는 검증 대상 회차 미포함.')
            available, reasons = number_availability(context, enabled)
            st.write(f'사용 가능한 번호 ({len(available)}개): ' + ', '.join(map(str, available)))
            st.caption('위 목록은 F01/F06에 의해 단일 번호로 제외되지 않은 번호입니다. 다른 필터는 조합 전체에 적용됩니다.')
            with st.expander('제외 번호와 사유', expanded=menu == '조건 설정'):
                if reasons:
                    st.dataframe([{'번호': n, '제외 사유': ' / '.join(why)} for n, why in reasons.items()], hide_index=True)
                else:
                    st.info('단일 번호 제외 없음')
            signature = (context, enabled, seed, collected_at)
            if run_slot.button(f'전체 조합 분석 및 {count}조합 추출', type='primary', width='stretch'):
                with st.spinner('8,145,060개 조합을 순차 분석 중입니다. 화면을 유지해주세요…'):
                    result = cached_analysis(context, enabled, seed)
                st.session_state.analysis_output = (signature, result)
            saved = st.session_state.get('analysis_output')
            if saved and saved[0] != signature:
                st.info('조건·시드·데이터 기준이 변경되었습니다. 실행 버튼으로 새 결과를 확인하세요.')
            elif saved:
                result = saved[1]
                counts = dict(result.group_counts)
                st.subheader('필터 처리 결과')
                a, b, c = st.columns(3)
                a.metric('전체 조합 수', f'{result.total:,}')
                b.metric('최종 잔여 조합 수', f'{result.remaining:,}')
                c.metric('계산 시간', f'{result.elapsed_seconds:.2f}초')
                st.caption(f'분석 시각: {result.analyzed_at} · 캐시 재사용 시 최초 계산 시간 표시')
                st.dataframe([{'조건': s.filter_id, '적용': 'ON' if s.enabled else 'OFF', '입력': s.entered,
                               '제거': s.removed, '통과': s.passed} for s in result.stages], hide_index=True)
                st.write(f"연속번호 정확히 1쌍: {counts['one_pair']:,}개 / 연속 없음: {counts['none']:,}개 / 2쌍 이상: {counts['multiple']:,}개")
                st.download_button('단계별 집계 CSV 저장', stages_csv(result), f'lotto_stages_{basis}.csv', 'text/csv')
                try:
                    selected = generate_numbers(result, count=count)
                except InsufficientCandidates as exc:
                    st.error(str(exc))
                else:
                    st.subheader(f'최종 {count}조합')
                    stats = coverage_stats(selected)
                    st.write(f"사용된 서로 다른 번호 {stats['distinct_numbers']}개 · 총 중복 수 {stats['total_duplicates']}개 ({6 * count} - 합집합 크기)")
                    st.caption('1조합: F13 적용 제외 · 무작위 추출' if count == 1 else '중복 최소화 시도 결과: 제한된 후보 풀에서 탐색하며 전역 최적을 보장하지 않습니다.' if 'F13' in config else 'F13 OFF: 기존 무작위 추출')
                    if count == 10:
                        st.caption('10조합은 번호 위치 60개에 숫자 45개를 사용하므로 모든 위치를 서로 다른 번호로 채울 수 없습니다.')
                    for i, combo in enumerate(selected, 1):
                        balls = ''.join(f'<span class="ball">{n}</span>' for n in combo)
                        shared = ', '.join(map(str, stats['shared_by_combo'][i - 1])) or '없음'
                        pattern = {'none': '없음', 'one_pair': '정확히 1쌍', 'multiple': '2쌍 이상'}[classify_consecutive(combo)]
                        st.markdown(f'<div class="pick-card"><b>조합 {i}</b><div class="pick-balls">{balls}</div>'
                                    f'<div class="pick-detail">연속번호: {pattern} · 다른 조합과 겹침: {shared}</div></div>',
                                    unsafe_allow_html=True)
                    st.dataframe([{'조합': i, '번호': ' · '.join(map(str, combo)), '연속번호':
                                   {'none': '없음', 'one_pair': '정확히 1쌍', 'multiple': '2쌍 이상'}[classify_consecutive(combo)],
                                   '다른 조합과 겹치는 번호': ', '.join(map(str, stats['shared_by_combo'][i - 1]))}
                                  for i, combo in enumerate(selected, 1)], hide_index=True)
                    st.download_button(f'최종 {count}조합 CSV 저장', results_csv(result, selected, collected_at),
                                       f'lotto_picks_{basis}_{seed}_{count}.csv', 'text/csv')
elif menu == '필터 분석':
    from analysis_ui import render_filter_analysis
    draws, status = read_snapshot(db.path)
    try:
        render_filter_analysis(draws, status, current_settings(), settings_store, render_backtest_period)
    except ValueError as exc:
        st.error('필터 분석 설정 오류: ' + str(exc))
else:
    st.title(menu)
    # 분석 메뉴는 mode=ro 스냅샷만 읽으며 Collector를 호출하지 않는다.
    draws, status = read_snapshot(db.path)
    collected_at = status.get('checked_at', '기록 없음')
    st.caption(f'최근 수집 확인 시점: {collected_at} · 읽기 전용 저장 데이터 {len(draws):,}회')
    if status and not status.get('ok'):
        st.warning('최근 수집 확인 실패: 기존 저장 데이터를 검증합니다. ' + status['message'])
    st.info('과거 조건 통과율은 예측 성공률이나 당첨 확률 향상을 의미하지 않습니다. 과거 데이터로 선택한 기준값의 성능을 미래 성능으로 단정할 수 없습니다. 실제 추출 조합의 당첨 성과를 검사하는 백테스트도 아닙니다.')
    st.caption('F01: 대상 직전 2회 / F02: 직전 1회 / F06: 직전 10회. 모든 계산에서 보너스 번호 제외. F03은 0~1쌍이면 단일 조합 적격이며 5조합 3:2, 10조합 6:4는 별도 구성 규칙입니다.')
    if not draws:
        st.error('검증할 저장 데이터가 없습니다.')
    else:
        start, end = render_backtest_period(draws)
        with st.expander('검증 필터 ON/OFF', expanded=True):
            enabled = render_controls('validation_toggle_', draws, start, end)
        mode = st.radio('검증 방식', ['회차별 검증', '과거 백테스트'], horizontal=True)
        if mode == '회차별 검증':
            target_round = st.selectbox('검증 대상 회차', [d.round for d in reversed(draws)])
            result = validate_round(draws, target_round, enabled)
            render_verdict(result)
            st.download_button('회차 검증 CSV 저장', rounds_csv((result,), collected_at), f'lotto_validation_{target_round}.csv', 'text/csv')
            render_precision(draws, target_round, enabled, 'round_precision')
        else:
            if start > end:
                st.error('시작 회차는 종료 회차보다 클 수 없습니다.')
            else:
                result = cached_backtest(draws, start, end, enabled)
                summary = result.summary
                st.subheader('백테스트 결과')
                a, b, c = st.columns(3)
                a.metric('활성 조건 전체 통과율', f"{summary['pass_rate']:.4%}" if summary['pass_rate'] is not None else '분석 불가')
                b.metric('통과 / 판정 가능', f"{summary['passed']} / {summary['evaluable']}")
                c.metric('분석 불가 (분모 제외)', summary['unavailable'])
                st.caption(f"요청 {summary['requested']}회 · 분석 불가 제외 · 계산 {result.elapsed_seconds:.3f}초 (캐시는 최초 계산 시간). 필터 변경 시 자동 재계산합니다.")
                filter_table = [{'조건': f['filter'], '적용': 'ON' if f['enabled'] else 'OFF', '통과': f['passed'],
                                 '실패': f['failed'], '판정 가능': f['evaluable'], '분석 불가': f['unavailable'],
                                 '통과율(%)': f['pass_rate'] * 100 if f['pass_rate'] is not None else None} for f in summary['filters']]
                st.dataframe(filter_table, hide_index=True)
                st.caption('조건별 통과율은 다른 조건과 무관한 독립 판정입니다. 각 조건의 판정 가능한 회차가 분모이며 OFF도 참고용 원래 조건을 표시합니다. 전체 통과율 분모는 활성 조건 모두 판정 가능한 회차입니다.')
                st.bar_chart(pd.DataFrame(filter_table).set_index('조건')['통과율(%)'])
                patterns = [{'연속번호': PATTERN_LABELS[p], '회차 수': count} for p, count in summary['patterns'].items()]
                st.subheader('실제 당첨번호 연속 패턴 빈도')
                st.caption(f"조건 통과 여부와 무관한 유효한 대상 당첨번호 {summary['pattern_denominator']}회 기준")
                st.dataframe(patterns, hide_index=True)
                st.bar_chart(pd.DataFrame(patterns).set_index('연속번호')['회차 수'])
                st.subheader('회차별 결과')
                compact = [{k: row[k] for k in ('회차', '추첨일', '당첨번호', '전체 판정', '연속번호', '실패 조건', '분석 불가 사유')}
                           for row in round_records(result.rows)]
                st.dataframe(compact, hide_index=True, use_container_width=True)
                st.download_button('백테스트 전체 결과 CSV 저장', rounds_csv(result.rows, collected_at), f'lotto_backtest_{start}_{end}.csv', 'text/csv')
                st.download_button('통과율 집계 CSV 저장', summary_csv(result), f'lotto_backtest_summary_{start}_{end}.csv', 'text/csv')
                target_round = st.selectbox('상세 검증 / 정밀 비교 회차', list(range(start, end + 1))[::-1])
                with st.expander('선택 회차 판정 근거', expanded=False):
                    render_verdict(result.rows[target_round - start])
                render_precision(draws, target_round, enabled, 'backtest_precision')

if menu in ('조건 설정', '번호 추출', '당첨번호 검증'):
    render_presets()
