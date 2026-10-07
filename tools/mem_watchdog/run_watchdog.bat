@echo off
rem Memory watchdog launcher (ASCII only - do not add non-ASCII chars).
rem start /b lets this batch return immediately, so the scheduled task instance
rem finishes instead of sitting in "running" state forever (which would stop
rem Task Scheduler from launching the every-minute self-healing instance).
start "" /b <data_drive>:/<miniconda_root>\pythonw.exe <project_root>\tools\mem_watchdog\watchdog.py
exit /b 0
