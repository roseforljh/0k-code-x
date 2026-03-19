@echo off
cd /d %~dp0
set "TOKEN_JSON_DIR=%~dp0codex_tokens"
python start_webui.py
