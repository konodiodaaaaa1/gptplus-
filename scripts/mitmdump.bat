@echo off
REM ?? mitmproxy, ?? src/addon.py ???? addon
REM ??: scripts\mitmdump.bat [port] [upstream_proxy]
setlocal
set PORT=%1
if "%PORT%"=="" set PORT=8888
set UPSTREAM=%2
if "%UPSTREAM%"=="" set UPSTREAM=http://127.0.0.1:7890

set IGNORE=googleapis\.com^|google\.com^|gstatic\.com^|googleusercontent\.com^|googlevideo\.com^|nie\.netease\.com^|netease\.com^|mumu

mitmdump -s "%~dp0..\src\addon.py" -p %PORT% --mode "upstream:%UPSTREAM%" --no-http2 --ignore-hosts "%IGNORE%"
endlocal
