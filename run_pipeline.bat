@echo off
cd /d \\172.30.1.47\work\naver_monitor
set LOG=pipeline_run_%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%.log
set LOG=%LOG: =0%

echo [%date% %time%] 파이프라인 시작 >> %LOG%

echo [1/4] 뉴스 모니터링 >> %LOG%
python naver_monitor.py >> %LOG% 2>&1
if errorlevel 1 echo [오류] naver_monitor.py 실패 >> %LOG%

echo [2/4] 단독기사 수집 >> %LOG%
python _collect_exclusive.py >> %LOG% 2>&1
if errorlevel 1 echo [오류] _collect_exclusive.py 실패 >> %LOG%

echo [3/4] 비제휴 언론사 수집 >> %LOG%
python _collect_nonpartner.py >> %LOG% 2>&1
if errorlevel 1 echo [오류] _collect_nonpartner.py 실패 >> %LOG%

echo [4/4] 삭제기사 점검 >> %LOG%
python _check_deleted.py >> %LOG% 2>&1
if errorlevel 1 echo [오류] _check_deleted.py 실패 >> %LOG%

echo [5/5] HTML 생성 및 배포 >> %LOG%
python make_html.py >> %LOG% 2>&1
if errorlevel 1 echo [오류] make_html.py 실패 >> %LOG%

git add docs/index.html >> %LOG% 2>&1
for /f "tokens=*" %%d in ('powershell -command "Get-Date -Format yyyy-MM-dd"') do set TODAY=%%d
for /f "tokens=*" %%h in ('powershell -command "Get-Date -Format HH:mm"') do set HHMM=%%h
git commit -m "auto: %TODAY% %HHMM% 모니터링 결과 갱신" >> %LOG% 2>&1
git push >> %LOG% 2>&1

echo [%date% %time%] 파이프라인 완료 >> %LOG%