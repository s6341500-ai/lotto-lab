"""필터 분석 메뉴. 기본 비교와 814만 조합 정밀 실행을 명시적 버튼으로 분리."""
from dataclasses import asdict
import pandas as pd
import streamlit as st

from experiments import (impact_analysis, ab_analysis, precise_ab, data_fingerprint,
                         ExperimentStore, impact_records, summary_records, analysis_csv, experiment_csv)
from settings import FilterSettings, ALL_FILTER_IDS
from filters import DESCRIPTIONS


@st.cache_data(show_spinner=False, max_entries=16)
def cached_impact(draws, start, end, config):
    return impact_analysis(draws, start, end, config)


@st.cache_data(show_spinner=False, max_entries=16)
def cached_ab(draws, start, end, a, b):
    return ab_analysis(draws, start, end, a, b)


@st.cache_data(show_spinner=False, max_entries=8)
def cached_precision(draws, target, a, b):
    return precise_ab(draws, target, a, b)


def _apply_editor(label, config):
    st.session_state[f'lab_{label}_config'] = asdict(config)
    for key in list(st.session_state):
        if key.startswith((f'lab_{label}_value_', f'lab_{label}_flag_')):
            del st.session_state[key]


def _editor(label, current, presets):
    model_key = f'lab_{label}_config'
    if model_key not in st.session_state:
        st.session_state[model_key] = asdict(current)
    model = st.session_state[model_key]

    def update_flag(fid):
        flags = set(st.session_state[model_key]['enabled_ids'])
        if st.session_state[f'lab_{label}_flag_{fid}']:
            flags.add(fid)
        else:
            flags.discard(fid)
        st.session_state[model_key]['enabled_ids'] = [f for f in ALL_FILTER_IDS if f in flags]

    def update_value(field):
        st.session_state[model_key][field] = st.session_state[f'lab_{label}_value_{field}']

    def load():
        try:
            source = st.session_state[f'lab_{label}_source']
            config = current if source == 'current' else FilterSettings() if source == 'factory' else FilterSettings.legacy() if source == 'legacy' else presets.load_preset(source[7:])
            _apply_editor(label, config)
        except (ValueError, OSError) as exc:
            st.session_state.lab_notice = str(exc)

    try:
        names = sorted(presets.read()['presets'])
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        names = []
    sources = ['current', 'factory', 'legacy'] + ['preset:' + n for n in names]
    st.selectbox(f'{label.upper()} 불러올 설정', sources, key=f'lab_{label}_source',
                 format_func=lambda v: {'current': '현재 조건 설정', 'factory': '공장 기본값', 'legacy': 'F01~F07만 ON'}.get(v, v[7:]))
    st.button(f'{label.upper()} 설정 불러오기', key=f'lab_{label}_load', on_click=load)
    with st.expander(f'{label.upper()} ON/OFF 및 수치 편집', expanded=False):
        for fid in ALL_FILTER_IDS:
            st.checkbox(f'{label.upper()} {fid} · {DESCRIPTIONS[fid]}', value=fid in model['enabled_ids'],
                        key=f'lab_{label}_flag_{fid}', on_change=update_flag, args=(fid,))
        fields = [('end_digit_max', 'F08 끝자리 최대', 1, 6), ('max_gap', 'F09 최대 간격', 1, 44),
                  ('low_boundary', 'F10 저번호 상한', 1, 44), ('low_min', 'F10 저번호 최소', 0, 6),
                  ('low_max', 'F10 저번호 최대', 0, 6), ('high_min', 'F10 고번호 최소', 0, 6),
                  ('high_max', 'F10 고번호 최대', 0, 6), ('similarity_threshold', 'F11 중복 임계값', 1, 6),
                  ('history_n', 'F11 최근 N회', 1, 100000), ('sum_min', 'F12 합계 최소', 21, 255),
                  ('sum_max', 'F12 합계 최대', 21, 255), ('pool_size', 'F13 후보 풀', 5, 2048),
                  ('optimization_trials', 'F13 시도 횟수', 1, 256)]
        for field, title, low, high in fields:
            st.number_input(f'{label.upper()} {title}', min_value=low, max_value=high, value=model[field], step=1,
                            key=f'lab_{label}_value_{field}', on_change=update_value, args=(field,))
        st.selectbox(f'{label.upper()} F11 이력 범위', ['all', 'recent'],
                     index=0 if model['history_scope'] == 'all' else 1,
                     format_func=lambda s: '전체 과거' if s == 'all' else '최근 N회',
                     key=f'lab_{label}_value_history_scope', on_change=update_value, args=('history_scope',))
    try:
        return FilterSettings.from_dict(st.session_state[model_key])
    except ValueError as exc:
        st.error(f'{label.upper()} 설정 오류: {exc}')
        return None


def _current_result(key, signature):
    saved = st.session_state.get(key)
    if saved and saved[0] != signature:
        st.info('기간·설정·데이터가 변경되어 이전 결과를 숨겼습니다. 분석 버튼을 다시 눌러주세요.')
        return None
    return saved[1] if saved else None


def _save_controls(result, store, key, precision=None):
    st.text_input('저장할 실험 이름', key=key + '_name')
    if st.button('실험 결과 저장', key=key + '_save'):
        try:
            record = store.save(st.session_state[key + '_name'], result, precision)
            st.success(f"실험 '{record['name']}'을 저장했습니다. 설정 프리셋은 변경하지 않았습니다.")
        except (ValueError, OSError) as exc:
            st.error(str(exc))


def _history(store, latest):
    try:
        records = store.read()['experiments']
    except (ValueError, OSError) as exc:
        st.error(str(exc))
        return
    if not records:
        st.info('저장된 실험이 없습니다. 영향도 또는 A/B 분석 후 실험 이름을 입력해 저장하세요.')
        return
    keys = sorted(records, key=lambda k: records[k]['saved_at'], reverse=True)
    title = lambda k: f"{records[k]['name']} · {records[k]['saved_at']} · {k[:6]}"
    selected = st.selectbox('저장 실험 열기', keys, format_func=title, key='lab_saved_selected')
    record = records[selected]
    result = record['result']
    st.caption(f"저장 결과 · {result['start']}~{result['end']}회 · 분석 {result['analyzed_at']} · 현재 데이터로 자동 재계산하지 않습니다.")
    st.dataframe(summary_records(result), hide_index=True)
    with st.expander('저장한 전체 설정 및 결과'):
        st.json(record)
    st.download_button('저장 실험 CSV', experiment_csv(record), f'experiment_{selected}.csv', 'text/csv')
    st.download_button('저장 실험 회차별 CSV', analysis_csv(result, True), f'experiment_rounds_{selected}.csv', 'text/csv')
    if record.get('precision'):
        _precision_table(record['precision'])
    else:
        st.caption('저장된 정밀 분석 없음: 후보 수와 후보 비율을 계산하지 않았습니다.')

    def restore():
        if result['end'] > latest:
            st.session_state.lab_notice = '저장 기간이 현재 DB 범위를 벗어나므로 설정을 불러오지 않았습니다.'
            return
        a = result['settings'] if result['kind'] == 'impact' else result['settings_a']
        b = a if result['kind'] == 'impact' else result['settings_b']
        _apply_editor('a', FilterSettings.from_dict(a))
        _apply_editor('b', FilterSettings.from_dict(b))
        st.session_state.selected_backtest_range = (result['start'], result['end'])
        st.session_state['backtest_period_0'] = result['start']
        st.session_state['backtest_period_1'] = result['end']
        st.session_state.lab_notice = '저장 설정과 기간을 A/B 편집기에 불러왔습니다. 비교 실행 버튼으로 현재 데이터에서 다시 분석하세요.'

    st.button('저장 설정·기간을 A/B에 불러오기', on_click=restore)
    confirm_key = 'lab_delete_confirm_' + selected
    confirmed = st.checkbox(f"'{record['name']}' 실험 삭제 확인", key=confirm_key)

    def delete():
        try:
            store.delete(selected, st.session_state.get(confirm_key, False))
            st.session_state.pop('lab_saved_selected', None)
            st.session_state.pop(confirm_key, None)
            st.session_state.lab_notice = '선택한 실험을 삭제했습니다.'
        except (ValueError, OSError) as exc:
            st.session_state.lab_notice = str(exc)

    st.button('선택 실험 삭제', disabled=not confirmed, on_click=delete)
    if len(keys) >= 2:
        st.subheader('저장 실험 나란히 비교')
        left = st.selectbox('기록 비교 1', keys, format_func=title, key='lab_history_left')
        right = st.selectbox('기록 비교 2', keys, index=1, format_func=title, key='lab_history_right')
        a, b = records[left]['result'], records[right]['result']
        if (a['start'], a['end'], a['data_fingerprint']) != (b['start'], b['end'], b['data_fingerprint']):
            st.warning('두 기록의 기간 또는 데이터가 다릅니다. 같은 분석 회차에 대한 직접 비교가 아닙니다.')
        st.dataframe([{'기록': records[k]['name'], '기간': f"{records[k]['result']['start']}~{records[k]['result']['end']}", **row}
                      for k in (left, right) for row in summary_records(records[k]['result'])], hide_index=True)


def _precision_table(result):
    st.caption(f"실제 정밀 계산 · 대상 {result['target_round']}회 · 공통 이력 기준 {result['basis_round']}회까지 · 총 {result['elapsed_seconds']:.2f}초")
    st.dataframe([{'설정': label.upper(), '전체 조합': result[label]['total'], '후보 수': result[label]['candidates'],
                   '후보 비율(%)': result[label]['candidate_rate'] * 100, '계산 시간(초)': result[label]['elapsed_seconds']}
                  for label in ('a', 'b')], hide_index=True)
    if any(result[label]['candidates'] == 0 for label in ('a', 'b')):
        st.warning('후보가 0개인 설정이 있습니다. 조건을 자동 완화하지 않으므로 설정을 직접 확인해주세요.')


def _independent_table(values):
    return [{'조건': r['filter'], '적용': 'ON' if r['enabled'] else 'OFF · 참고용',
             '통과': r['passed'], '분석 회차': r['evaluable'], '분석 불가': r['unavailable'],
             '독립 통과율(%)': r['pass_rate'] * 100 if r['pass_rate'] is not None else None} for r in values]


def _ab_round_table(values):
    labels = {'both': '양쪽 통과', 'a_only': 'A만 통과', 'b_only': 'B만 통과',
              'neither': '양쪽 실패', 'unavailable_either': '한쪽 이상 분석 불가'}
    verdict = lambda v: '분석 불가' if v is None else '통과' if v else '실패'
    return [{'회차': r['round'], '추첨일': r['date'], '당첨번호': ' · '.join(map(str, r['numbers'])),
             'A 판정': verdict(r['a']), 'B 판정': verdict(r['b']), '분류': labels[r['group']],
             'A 실패 조건': ', '.join(r['failed_a']), 'B 실패 조건': ', '.join(r['failed_b'])} for r in values]


def render_filter_analysis(draws, status, current, presets, period_renderer):
    st.title('필터 분석')
    st.info('분석은 과거 데이터에 대한 설명입니다. 통과율 상승은 미래 당첨 확률 향상이나 예측 성공을 의미하지 않습니다.')
    st.caption('실제 당첨번호의 보너스 제외 · 대상 회차 이전 이력만 참조 · F03은 단일 조합의 연속 0~1쌍 적격 판정 · F13과 추출 개수별 구성 규칙은 통과율 분석에서 제외')
    st.caption(f"최근 수집 확인: {status.get('checked_at', '기록 없음')} · 분석은 읽기 전용")
    if status and not status.get('ok'):
        st.warning('최근 수집 실패 상태이므로 기존 저장 데이터만 분석합니다.')
    if not draws:
        st.error('분석할 당첨 데이터가 없습니다. 대시보드에서 수집 상태를 확인하세요.')
        return
    start, end = period_renderer(draws)
    valid_period = start <= end
    store = ExperimentStore(presets.path.parent / 'experiments.json')
    fingerprint = data_fingerprint(draws, end)
    if st.session_state.get('lab_notice'):
        st.info(st.session_state.lab_notice)
    impact_tab, ab_tab, history_tab = st.tabs(['조건별 영향도', 'A-B 비교', '실험 기록'])
    with impact_tab:
        st.caption('현재 조건 설정 메뉴의 설정을 사용합니다. 독립 통과율은 해당 조건 하나의 판정이고, 영향도는 나머지 조건을 유지한 채 한 조건을 제거했을 때 전체 통과율의 변화입니다.')
        with st.expander('기준 설정 스냅샷'):
            st.json(asdict(current))
        signature = (fingerprint, start, end, current)
        if st.button('조건별 영향도 분석 실행', disabled=not valid_period):
            try:
                with st.spinner('과거 당첨번호 판정과 필터 OFF 시나리오 계산 중…'):
                    result = cached_impact(tuple(draws), start, end, current)
                st.session_state.lab_impact_result = (signature, result)
            except ValueError as exc:
                st.error('영향도 분석 불가: ' + str(exc))
        result = _current_result('lab_impact_result', signature)
        if result:
            st.subheader('영향도 결과')
            if result['baseline']['evaluable'] == 0:
                st.warning('기준 설정을 판정할 수 있는 회차가 없습니다. 필요한 과거 이력과 선택 기간을 확인하세요.')
            st.caption(f"{start}~{end}회 · 분석 {result['analyzed_at']} · 계산 {result['elapsed_seconds']:.3f}초 (캐시는 최초 계산 시간)")
            order = st.selectbox('영향도 정렬', ['통과율 증가순', '변화 절댓값순', '필터 번호순'])
            rows = impact_records(result)
            if order != '필터 번호순':
                rows = rows[:1] + sorted(rows[1:], key=lambda r: (r['차이(%p)'] is None, -(abs(r['차이(%p)']) if order == '변화 절댓값순' else r['차이(%p)']) if r['차이(%p)'] is not None else 0))
            st.dataframe(rows, hide_index=True)
            st.caption('기준·시나리오의 분석 가능 분모가 다를 수 있습니다. 공통 판정 회차 차이도 함께 확인하세요. 분석 불가는 실패로 세지 않습니다.')
            st.write('이미 OFF · 영향도 제외: ' + (', '.join(result['already_off']) or '없음') + ' · F13 항상 제외')
            if result['scenarios']:
                st.bar_chart(pd.DataFrame(rows[1:]).set_index('시나리오')['차이(%p)'])
            else:
                st.info('활성 조합 필터가 없어 OFF 시나리오가 없습니다.')
            st.dataframe(_independent_table(result['independent_rates']), hide_index=True)
            st.download_button('영향도 요약 CSV', analysis_csv(result), 'filter_impact.csv', 'text/csv')
            st.download_button('영향도 회차별 CSV', analysis_csv(result, True), 'filter_impact_rounds.csv', 'text/csv')
            _save_controls(result, store, 'lab_impact')
    with ab_tab:
        left, right = st.columns(2)
        with left:
            a = _editor('a', current, presets)
        with right:
            b = _editor('b', current, presets)
        signature = (fingerprint, start, end, a, b)
        if a and b:
            with st.expander('설정 차이 요약', expanded=True):
                from experiments import settings_differences
                differences = settings_differences(a, b)
                st.dataframe(differences, hide_index=True) if differences else st.write('A와 B의 설정이 같습니다.')
        if st.button('A/B 비교 실행', disabled=not valid_period or a is None or b is None):
            try:
                with st.spinner('동일 기간의 실제 당첨번호 A/B 비교 중…'):
                    result = cached_ab(tuple(draws), start, end, a, b)
                st.session_state.lab_ab_result = (signature, result)
            except ValueError as exc:
                st.error('A/B 분석 불가: ' + str(exc))
        result = _current_result('lab_ab_result', signature)
        precision = None
        if result:
            st.subheader('A/B 비교 결과')
            if result['a']['evaluable'] == 0 or result['b']['evaluable'] == 0:
                st.warning('한쪽 이상 설정의 판정 가능 회차가 없습니다. 분석 불가 회차는 통과율 분모에서 제외했습니다.')
            st.caption(f"동일 기간 {start}~{end}회 · 분석 {result['analyzed_at']} · 계산 {result['elapsed_seconds']:.3f}초")
            st.dataframe(summary_records(result), hide_index=True)
            overlap = result['overlap']
            st.write(f"A만 통과 {overlap['a_only']}회 · B만 통과 {overlap['b_only']}회 · 양쪽 통과 {overlap['both']}회 · 양쪽 실패 {overlap['neither']}회")
            st.caption(f"교차 집계는 양쪽 모두 판정 가능한 {overlap['common_evaluable']}회만 포함합니다. 한쪽 이상 분석 불가 {overlap['unavailable_either']}회. A 통과/B 불가 {overlap['a_pass_b_unavailable']}회, B 통과/A 불가 {overlap['b_pass_a_unavailable']}회.")
            st.dataframe(_ab_round_table(result['rows']), hide_index=True)
            with st.expander('조건별 독립 통과율 (전체 통과율 영향도와 다른 지표)'):
                st.dataframe([{'조건': x['filter'], 'A 적용': x['enabled'], 'A 통과율(%)': x['pass_rate'] * 100 if x['pass_rate'] is not None else None,
                               'A 분모': x['evaluable'], 'B 적용': y['enabled'], 'B 통과율(%)': y['pass_rate'] * 100 if y['pass_rate'] is not None else None,
                               'B 분모': y['evaluable']} for x, y in zip(result['independent_a'], result['independent_b'])], hide_index=True)
            st.download_button('A/B 요약 CSV', analysis_csv(result), 'filter_ab_summary.csv', 'text/csv')
            st.download_button('A/B 회차별 CSV', analysis_csv(result, True), 'filter_ab_rounds.csv', 'text/csv')
            target = st.selectbox('A/B 정밀 비교 대상 회차', [d.round for d in reversed(draws)], key='lab_precision_target')
            precision_signature = (signature, target)
            if st.button('A/B 전체 조합 정밀 비교 실행'):
                try:
                    with st.spinner('대상 직전 기준으로 A/B 전체 조합을 각각 실제 순회 중…'):
                        precision_result = cached_precision(tuple(draws), target, a, b)
                    st.session_state.lab_precision_result = (precision_signature, precision_result)
                except ValueError as exc:
                    st.error('정밀 분석 불가: ' + str(exc))
            precision = _current_result('lab_precision_result', precision_signature)
            if precision:
                _precision_table(precision)
                from experiments import records_csv
                st.download_button('A/B 정밀 비교 CSV', records_csv([{'설정': label.upper(), '대상 회차': target,
                                   '기준 회차': precision['basis_round'], '분석 시각': precision['analyzed_at'],
                                   '설정 스냅샷': precision['settings_' + label], **precision[label]} for label in ('a', 'b')]), 'filter_ab_precision.csv', 'text/csv')
            else:
                st.caption('현재 설정의 정밀 분석 미실행: 후보 수와 후보 비율을 계산하지 않았습니다.')
            _save_controls(result, store, 'lab_ab', precision)
        else:
            st.caption('비교 실행 전에는 후보 수와 후보 비율을 표시하지 않습니다.')
    with history_tab:
        _history(store, draws[-1].round)
