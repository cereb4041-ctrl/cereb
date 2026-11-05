@echo off
rem ================================================================
rem  JV-Link 時系列オッズ収集 起動バッチ
rem    run_collector.bat            当日ループ（朝～最終レース発走まで）
rem    run_collector.bat collect    1 回だけ取得（10 分おきのタスク用）
rem  PYTHON_EXE に 32bit 版 Python のパスを設定してください。
rem ================================================================
setlocal
if "%PYTHON_EXE%"=="" set PYTHON_EXE=py -3-32
set CMD=%1
if "%CMD%"=="" set CMD=run
rem jv_odds フォルダの 1 つ上（パッケージの親）をカレントにする
cd /d "%~dp0..\.."
%PYTHON_EXE% -m jv_odds %CMD% %2 %3 %4
exit /b %ERRORLEVEL%
