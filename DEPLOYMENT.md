# Lotto Lab 모바일·클라우드 배포 안내

이 문서는 배포 **준비** 안내입니다. GitHub 업로드, 외부 배포, 앱 공개는 수행하지 않았습니다. 아래 외부 서비스 작업은 사용자가 내용을 확인하고 승인한 뒤 진행합니다.

## 추천 방식과 한계

개인용 비공개 앱 1개를 간헐적으로 사용하는 경우 Streamlit Community Cloud 무료 배포를 우선 권장합니다. PC가 꺼져도 클라우드에서 실행됩니다. Android 앱 설치 파일은 필요하지 않습니다. 휴대전화 브라우저의 홈 화면에 추가할 수 있지만, 오프라인 앱은 아닙니다.

2026-10-22 공식 문서 확인:

* [앱 공개 범위](https://docs.streamlit.io/deploy/streamlit-community-cloud/share-your-app): 비공개 저장소에서 배포한 앱은 기본 비공개이며, 비공개 앱은 한 번에 1개만 허용됩니다. Settings → Sharing → **Only specific people can view this app**를 확인하세요. 공개 저장소의 앱은 기본 공개입니다. URL을 숨기는 것만으로 접근을 제한하지 않습니다.
* 비공개 앱 사용자는 본인의 계정으로 로그인합니다. 별도 초대자는 Google 로그인 또는 이메일 일회용 링크를 사용할 수 있습니다. 이 앱은 사용자별 저장소 분리를 구현하지 않으므로 본인만 접근하게 설정하세요.
* [자원·절전](https://docs.streamlit.io/deploy/streamlit-community-cloud/manage-your-app): 12시간 미사용 시 절전되며 접근 가능한 사용자가 깨울 수 있습니다. 24시간 무중단·즉시 응답 서비스는 아닙니다. 문서의 자원 수치는 **2024년 2월 기준 근사치**(CPU 0.078~2 cores, 메모리 690MB~2.7GB, 저장소 최대 50GB)이며 현재 보장량이 아닙니다. 실제 할당량은 계정 설정에서 확인해야 합니다. CPU 제한 시 전체 조합 계산은 로컬보다 오래 걸릴 수 있습니다.
* [파일 저장](https://docs.streamlit.io/develop/concepts/connections/connecting-to-data): 로컬 파일은 언제든 사라질 수 있습니다. SQLite·JSON을 썼다는 이유로 영구 저장되지 않습니다.
* [배포 및 Python 선택](https://docs.streamlit.io/deploy/streamlit-community-cloud/deploy-your-app/deploy): Advanced settings에서 **Python 3.11**을 선택하세요. 이 프로젝트는 3.11.9로 검증했습니다. 플랫폼 기본 Python은 문서상 3.12이며 향후 달라질 수 있습니다. `runtime.txt`에 의존하지 않습니다.
* [플랫폼 제한](https://docs.streamlit.io/deploy/streamlit-community-cloud/status): 미국에서 호스팅됩니다. 동행복권 API가 클라우드 IP를 차단하는지는 실제 배포 후 확인해야 합니다. 실패해도 기존 당첨 데이터는 유지하며 오류를 표시합니다. 일부 Streamlit 설정은 플랫폼에서 강제 덮어씁니다.

자동 영구 저장과 상시 가동이 필수라면 무료 Community Cloud는 요구에 맞지 않습니다. 간단한 대안은 **유료 Render Web Service + Persistent Disk**이며 `LOTTO_LAB_DATA_DIR`를 디스크 마운트 경로로 지정하면 기존 SQLite/JSON 구조를 유지할 수 있습니다. [Render 무료 서비스](https://render.com/docs/free)는 디스크 영구 저장을 제공하지 않으므로 대안이 되지 않습니다. [영구 디스크](https://render.com/docs/disks)는 유료이며 별도 백업도 필요합니다. 대안 호스팅에는 본인 전용 인증을 추가해야 하므로, 이번에는 설정·결제·외부 연동을 구현하지 않았습니다. 비용은 실제 신청 시 확인하세요.

## 데이터 보존 설계

* 원본 `data/lotto.db`는 업로드 대상에서 제외합니다. 작업 전 로컬 사본은 `artifacts/backups/lotto-original.db`, 원본 해시는 `artifacts/baseline.json`에 있습니다. 이 파일도 업로드하지 않습니다.
* 공개 당첨 사실(회차·날짜·6개 번호·보너스)만 담은 `seed/draws.json`에 1~1246회가 들어 있습니다. 개인 설정·메타데이터·실험 기록은 포함하지 않습니다.
* 기본 DB가 없는 새 환경에서는 전체 초기 데이터를 검증한 뒤 단일 트랜잭션으로 새 DB에 넣습니다. 기존 DB는 덮어쓰지 않습니다. 명시적으로 지정한 임의 테스트 DB에는 자동 초기 데이터를 넣지 않습니다.
* 앱 첫 대시보드 접속 시 기존 수집기가 최신 회차를 확인하며 수동 새로고침도 유지합니다. 이것은 예약 작업이 아닙니다. 앱 절전 중에는 자동 수집이 실행되지 않습니다. 초기 데이터가 1246회라는 사실이 현재 최신 회차를 의미하지는 않습니다.
* DB가 삭제되면 초기 1246회로 복구하고, 이후 회차는 API로 다시 수집합니다. API 실패 시 초기 데이터 이후의 최신성은 보장하지 않습니다. 로컬 원본 DB는 별도로 계속 보관하세요.
* 프리셋·기본값·실험은 사이드바 **개인 데이터 백업 / 복원**에서 JSON으로 내려받아 휴대전화/PC/개인 드라이브에 보관합니다. 변경 후 매번 백업하세요. 재시작 후 필요하면 파일을 선택하고 덮어쓰기 확인 → 백업 복원을 누릅니다. 복원 후 브라우저를 새로고침하면 저장된 기본 설정을 다시 읽습니다. 이 기능은 당첨 DB를 변경하지 않습니다. 자동 외부 영구 저장은 제공하지 않습니다.
* 클라우드와 PC의 개인 파일은 자동 동기화되지 않습니다. 공용 앱으로 열면 방문자들이 같은 설정·기록 파일을 공유하므로 권장하지 않습니다.

## 로컬 검증과 배포 묶음 만들기

표준 Python 3.11 이상을 설치한 새 PC의 PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m streamlit run app.py
```

현재 PC에서는 기존 명령 ` .\.runtime\python\python.exe -m streamlit run app.py `도 계속 사용할 수 있습니다. 서버 코드에는 해당 Windows 실행 경로 의존성이 없습니다. Linux에서는 `python -m pip install -r requirements.txt`와 `python -m streamlit run app.py --server.address=0.0.0.0`을 사용합니다. Linux에서의 실제 실행은 별도 검증 대상입니다.

배포 묶음을 새 폴더로 생성하려면:

```powershell
.\.runtime\python\python.exe scripts/build_cloud_bundle.py
```

이미 `dist/lotto-lab-cloud`가 있다면 기존 폴더를 자동 삭제하지 않습니다. 새 경로를 지정하세요:

```powershell
.\.runtime\python\python.exe scripts/build_cloud_bundle.py dist/lotto-lab-cloud-v2
```

생성된 폴더에는 앱의 Python 모듈, 두 requirements 파일, 문서, `.streamlit/config.toml`, 초기 당첨 데이터, `data/.gitkeep`, 파일별 SHA-256 manifest만 들어갑니다. 로컬 테스트·프로파일 도구는 원본 프로젝트에 남습니다. `.runtime`, DB, 개인 설정, 실험 기록, API 원문, 보고서, 백업, 로그, `.env`, `secrets.toml`, 계정 정보는 업로드하지 않습니다. 현재 앱은 API 키를 요구하지 않습니다. 새 키가 필요해지면 GitHub 파일이 아닌 Community Cloud Secrets에 저장하세요.

## 승인 후 GitHub에 올리는 순서

1. GitHub에 로그인하고 **New repository**를 선택합니다. 이름은 `lotto-lab`, 공개 범위는 **Private**를 권장합니다.
2. **Create repository**를 누릅니다. 빈 저장소에서 **uploading an existing file** 또는 **Add file → Upload files**를 선택합니다.
3. 원본 프로젝트 전체가 아니라 위에서 만든 **배포 묶음 폴더 안의 파일과 폴더**를 올립니다. `app.py`가 저장소 최상위에 있도록 합니다. `lotto-lab-cloud/app.py`처럼 폴더째 한 단계 깊어지지 않게 확인하세요.
4. Windows 탐색기에서 숨김 항목을 표시하고 `.streamlit/config.toml`, `.gitignore`, `data/.gitkeep`가 포함됐는지 확인합니다. 빠졌다면 GitHub의 **Add file → Create new file**에서 해당 경로와 내용을 그대로 추가합니다. 빈 `data` 폴더는 없어도 앱이 생성합니다.
5. 업로드 목록에 `.db`, `.runtime`, `settings.json`, `experiments.json`, `secrets.toml`, `.env`, `artifacts`가 없는지 확인한 후 **Commit changes**를 누릅니다. 파일을 잘못 올렸다면 배포 전에 정리합니다. 비밀번호·키를 올렸다면 삭제만으로 기록에서 사라지지 않으므로 키도 교체해야 합니다.

## 승인 후 Community Cloud 배포 순서

1. [share.streamlit.io](https://share.streamlit.io)에 로그인하고 GitHub 저장소 접근을 연결합니다. 비공개 저장소 접근 권한을 검토합니다.
2. **Create app → Yup, I have an app**을 선택합니다.
3. 저장소 `본인계정/lotto-lab`, 실제 브랜치(대개 `main`), Main file path `app.py`를 입력합니다.
4. **Advanced settings → Python version → 3.11**을 선택하고 저장합니다. API 키는 현재 필요 없습니다.
5. **Deploy**를 누른 뒤 설치·시작 로그를 확인합니다. `.runtime`이나 APK 파일을 업로드하지 않습니다.
6. **Settings → Sharing → Only specific people can view this app**를 확인합니다. 본인 이외의 viewer는 추가하지 않는 것을 권장합니다. 비공개 저장소와 앱 접근 설정을 각각 확인하세요.
7. 대시보드에서 1246회 초기 데이터, API 업데이트 상태, 수동 업데이트를 확인합니다. 클라우드 API가 실패하면 성공으로 간주하지 말고 저장 데이터 기준으로 사용합니다.
8. 휴대전화에서 `https://선택주소.streamlit.app`에 로그인합니다. 사이드바 → 번호 추출 → 1/5/10조합 선택 → 필요 시 조건 펼치기 → 분석 버튼을 누릅니다. 결과는 번호 카드와 CSV로 확인합니다. 브라우저 메뉴에서 **홈 화면에 추가**하면 다시 접속하기 편합니다.
9. PC를 끈 상태에서 접속·추출·백업을 확인합니다. 앱을 절전 후 깨우고 데이터 복구·개인 JSON 복원도 확인합니다. 이 단계까지 확인해야 실제 배포 검증이 완료됩니다.

## 성능과 캐시

전체 8,145,060개 조합은 서버에서 계산합니다. 휴대전화가 전체 조합을 저장하지 않습니다. 스트리밍 집계·제한된 reservoir/후보 풀을 유지하고, 한 Python 프로세스 안에서 전체 조합 분석은 한 번에 하나씩 실행합니다. 다중 프로세스·다중 서버에 걸친 분산 잠금은 아닙니다. 대기 중 화면을 유지하세요. CPU 할당량에 따라 대기 시간이 길어질 수 있습니다.

계산은 실행 버튼으로만 시작합니다. 입력 데이터·기준 회차·조건·시드를 캐시 키에 포함하며 변경된 설정에 이전 결과를 새 결과처럼 표시하지 않습니다. 1/5/10 선택은 같은 후보 집계를 재사용합니다. F03/F13 규칙과 기존 필터 판정은 유지합니다. 정밀 A/B 분석은 별도 버튼으로 실행합니다. 캐시 수는 제한되어 있지만 많은 설정/세션을 동시에 사용하면 메모리 사용량이 늘 수 있습니다.

개발 검증 도구:

```powershell
.\.runtime\python\python.exe scripts/profile_cloud.py
.\.runtime\python\python.exe scripts/profile_cloud.py --all-off
# 선택: 실제 Chrome 기반 모바일 크기 테스트
.\.runtime\python\python.exe -m pip install playwright
.\.runtime\python\python.exe scripts/check_mobile.py
```

Windows의 Chrome 기본 설치 경로를 사용합니다. 다른 환경은 `LOTTO_LAB_CHROME`에 Chromium 실행 파일 경로를 지정하거나 Playwright Chromium을 설치합니다. 모바일 검사는 별도의 임시 DB와 `LOTTO_LAB_AUTO_UPDATE=0`을 사용해 원본 DB와 외부 API 요청을 건드리지 않습니다. 프로파일의 로컬 Windows 측정은 무료 클라우드에서의 시간·메모리·타임아웃 보장이 아닙니다.

환경 변수 `LOTTO_LAB_DATA_DIR`로 별도 데이터 폴더를 지정할 수 있습니다. 지정하지 않으면 프로젝트의 `data/`를 사용합니다. `LOTTO_LAB_AUTO_UPDATE=0`은 접속 시 자동 확인만 끄며 수동 업데이트 버튼은 유지합니다.


### 수집·복원 수정 (2026-10-30)

`seed/draws.json`은 보존하고 공식 전체 조회와 대조한 `seed/draws-001247.json`을 추가했습니다. 배포할 저장소에 두 파일 모두 포함하세요. 기본 DB는 최초 생성뿐 아니라 빈 DB 및 누락 회차도 검증된 스냅샷에서 복원합니다. 기존 회차와 충돌하면 덮어쓰지 않습니다. `LOTTO_LAB_DATA_DIR`을 지정해도 기본 DB에 동일한 복원이 적용됩니다. 명시적으로 지정한 별도 테스트 DB는 자동 복원하지 않습니다.

공식 조회는 `lt645/selectPstLt645Info.do`(최신/전체), `lt645/selectPstLt645InfoNew.do`(과거 회차/대체 최근 조회)를 사용합니다. 후자는 주변 회차도 반환하므로 요청한 회차만 선택합니다. 모든 회차는 추첨일·번호 범위·중복을 검증합니다. 정상적인 연속 데이터가 있으면 최신 조회 및 신규·누락 회차만 요청합니다. 실패해도 저장된 데이터와 마지막 성공 시점을 유지합니다.

Streamlit Cloud의 로컬 SQLite는 영구 저장소가 아닙니다. 재배포 시 보장되는 복원 범위는 GitHub에 커밋한 스냅샷까지입니다. 실행 중 수집한 추가 회차는 GitHub에 자동 저장되지 않으며 재시작 후 온라인 재수집이 필요합니다. 복원 기준을 올리려면 신뢰할 수 있는 환경에서 공식 전체 결과를 검증하고 새 `seed/draws-XXXXXX.json`을 추가해 커밋하며, `scripts/build_cloud_bundle.py`의 배포 목록에도 추가하세요. 기존 스냅샷을 덮어쓰지 마세요.

새 배포 폴더는 `.runtime/python/python.exe scripts/build_cloud_bundle.py dist/lotto-lab-recovery-1247-v2`로 만들었습니다. 기존 dist는 보존했습니다. 실제 Cloud 재배포 후 대시보드에서 저장 수·최신 회차·누락 수·출처·갱신 결과를 확인하세요.
