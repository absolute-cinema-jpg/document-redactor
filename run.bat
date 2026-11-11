@echo off
rem Starts the Document Redactor in your browser (Windows, for development).
cd /d "%~dp0"
if not exist .venv-win\Scripts\python.exe (
  echo First run: setting up a private Python environment...
  py -3 -m venv .venv-win || python -m venv .venv-win
  .venv-win\Scripts\python.exe -m pip install -q --upgrade pip
  .venv-win\Scripts\python.exe -m pip install -q -r requirements.txt
)
.venv-win\Scripts\python.exe -m redactor %*
