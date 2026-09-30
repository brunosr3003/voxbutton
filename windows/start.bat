@echo off
rem Starts the voxbutton server (minimized console, for its log) and the button.
rem Needs uv (https://docs.astral.sh/uv/) and Tailscale. Run install-startup.bat
rem once to have this start with Windows.
cd /d "%~dp0..\server"
start "voxbutton server" /min uv run voxbutton-server
rem Give the server a head start; the button retries until it answers anyway.
timeout /t 3 /nobreak >nul
start "" uv run pythonw "%~dp0..\desktop\voxbutton_tk.py"
