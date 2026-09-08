@echo off
rem Uruchamia ZeroTier Alternative UI bez okna konsoli.
cd /d "%~dp0"
start "" pythonw "%~dp0zerotier_ui.py"
