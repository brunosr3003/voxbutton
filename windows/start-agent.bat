@echo off
rem Lends this PC's microphone to a voxbutton server on another machine.
rem Needs Python 3 (python.org) and %APPDATA%\voxbutton\config.json with
rem {"server": "http://<server>:8765", "token": "..."}. To start it with
rem Windows instead: python "%~dp0..\agent\voxbutton_agent.py" --install
python "%~dp0..\agent\voxbutton_agent.py" %*
