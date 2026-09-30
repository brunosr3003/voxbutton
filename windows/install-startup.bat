@echo off
rem Adds a shortcut to start.bat in the Startup folder, so voxbutton starts with Windows.
set "LINK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\voxbutton.lnk"
powershell -NoProfile -Command ^
  "$s = (New-Object -ComObject WScript.Shell).CreateShortcut('%LINK%');" ^
  "$s.TargetPath = '%~dp0start.bat'; $s.WorkingDirectory = '%~dp0'; $s.WindowStyle = 7; $s.Save()"
echo Added %LINK%
