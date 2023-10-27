@echo off
call .\JiraCheckTool-Venv\Scripts\activate
.\JiraCheckTool-Venv\Scripts\python.exe .\src\jira_check.py
pause
