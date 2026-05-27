# 네이버 뉴스 모니터링 시스템 운영 가이드

## 시스템 위치

| 항목 | 경로 |
|------|------|
| 소스코드 + DB | `\\172.30.1.47\work\naver_monitor\` |
| DB 파일 | `\\172.30.1.47\work\naver_monitor\naver_monitor.db` |
| 결과 HTML | `\\172.30.1.47\work\naver_monitor\docs\index.html` |
| git 원격 | `https://github.com/risky779/naver_news` |
| GitHub Pages | 위 저장소의 `docs/index.html` |

---

## 자동 실행 (Task Scheduler)

매일 2회 자동 실행됨:

| 작업명 | 실행 시간 | 상태 |
|--------|-----------|------|
| `NaverNewsMonitor_1100` | 오전 11:00 | Ready |
| `NaverNewsMonitor_1600` | 오후 4:00 | Ready |

실행 명령: `cmd.exe /c "\\172.30.1.47\work\naver_monitor\run_pipeline.bat"`

> **주의:** 이 PC가 켜져 있고 `\\172.30.1.47` 네트워크 드라이브가 접근 가능해야 자동 실행됨.

---

## 파이프라인 순서 (`run_pipeline.bat`)

```
1. naver_monitor.py        — 98개 제휴 언론사 기사 수집 + 위반 분석
2. _collect_exclusive.py   — [단독] 기사 수집 (네이버 + Google RSS)
3. _collect_nonpartner.py  — 111개 비제휴 언론사 RSS 수집
4. _check_deleted.py       — 전체 기사 URL 삭제 여부 확인
5. make_html.py            — 결과 HTML 생성
   git commit + push       — docs/index.html 자동 배포
```

실행 시간: 전체 약 1~2시간 소요 (4단계 URL 검사가 가장 오래 걸림)

---

## 수동 실행 방법

**전체 파이프라인 한 번에 실행:**

```bat
cmd.exe /c "\\172.30.1.47\work\naver_monitor\run_pipeline.bat"
```

**단계별 개별 실행 (cmd에서):**

```bat
cd \\172.30.1.47\work\naver_monitor
python naver_monitor.py
python _collect_exclusive.py
python _collect_nonpartner.py
python _check_deleted.py
python make_html.py
git add docs/index.html && git commit -m "수동 실행" && git push
```

---

## 파이프라인 스크립트 역할 상세

| 스크립트 | 역할 | 소요 시간 |
|----------|------|-----------|
| `naver_monitor.py` | 언론사 98곳 기사 샘플링 → B/C/E/J/L항 위반 검사 → `press_ranking.json` 갱신 | ~20분 |
| `_collect_exclusive.py` | [단독] 태그 기사 수집. 네이버 모바일 검색(10페이지) + Google RSS | ~10분 |
| `_collect_nonpartner.py` | 비제휴 지방지·전문지 111곳 Google RSS 수집 | ~30분 |
| `_check_deleted.py` | DB 전체 기사 URL 접속 → 삭제 여부 분류 (5가지 유형) | ~1시간 |
| `make_html.py` | DB → `docs/index.html` 생성 (언론사 랭킹 + 삭제 의심 목록) | ~1분 |
| `_resolve_daum.py` | v.daum.net 기사에서 원본 언론사 URL 추출 (수동 보조) | — |
| `_resolve_unknown.py` | 삭제 분류 미확인 기사 재분석 (수동 보조) | — |

---

## 삭제 유형 분류 기준

| 코드 | 유형 | 의미 |
|------|------|------|
| 1 | 네이버만삭제 | 네이버 링크는 죽었지만 언론사 원문 존재 |
| 2 | 완전삭제 | 언론사 원문도 사라짐 |
| 3 | 출처미확인 | 언론사 도메인 특정 불가 |
| 4 | 언론사직접삭제 | 언론사 원본 URL로 직접 수집했는데 삭제됨 |
| 5 | 링크삭제 | 제목 검색으로 기사 확인 불가 |

---

## 현재 DB 현황 (2026-05-27 기준)

| 항목 | 건수 |
|------|------|
| 전체 수집 기사 | 20,865건 |
| 삭제 의심 | 30건 |
| [단독] 기사 | 1,871건 |

---

## 로그 확인

파이프라인 실행 시 자동으로 날짜별 로그 파일 생성:

```
\\172.30.1.47\work\naver_monitor\pipeline_run_YYYYMMDD_HHMM.log
```

---

## 이슈 발생 시 대응

| 증상 | 원인 | 조치 |
|------|------|------|
| Task Scheduler 실패 (exit 255) | LF 줄바꿈 또는 경로 문제 | `.bat` 파일 CRLF 확인, 작업 디렉토리 설정 확인 |
| DB 손상 (`malformed`) | 실행 중 복사 등 동시 접근 | `sqlite3.exe .recover` 후 재구성 |
| 삭제 오탐 (`링크삭제`) | 다른 언론사 동명 기사 검색 | `_check_deleted.py`의 도메인 검증 로직이 처리함 |
| git push 실패 | 인증 또는 upstream 미설정 | `git push --set-upstream origin main` |
| 네트워크 드라이브 미연결 | 재부팅 후 자동 연결 안 됨 | `net use Z: \\172.30.1.47\work\naver_monitor` |

---

## 환경 설정 (신규 PC 세팅 시)

### 필수 패키지
```bat
pip install aiohttp playwright python-dotenv
playwright install chromium
```

### .env 파일 위치
```
\\172.30.1.47\work\naver_monitor\.env
```
포함 항목: `NAVER_CLIENT_ID`, `NAVER_CLIENT_SECRET`, `NAVER_SEARCH_CLIENT_ID`, `NAVER_SEARCH_CLIENT_SECRET`

> **.env는 절대 git에 커밋하지 않을 것** (`.gitignore`에 등록됨)

### git 사용자 설정
```bat
cd \\172.30.1.47\work\naver_monitor
git config user.email "risky779@gmail.com"
git config user.name "risky779"
```

### Task Scheduler 등록
- 작업 이름: `NaverNewsMonitor_1100` / `NaverNewsMonitor_1600`
- 동작: `cmd.exe /c "\\172.30.1.47\work\naver_monitor\run_pipeline.bat"`
- 시작 위치: `\\172.30.1.47\work\naver_monitor`
- 트리거: 매일 11:00 / 16:00
