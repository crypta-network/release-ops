@echo off
setlocal enableextensions
set DIR=%~dp0
set CP=%DIR%..\\lib\\*
set JAVA_EXE=%JAVA_HOME%\\bin\\java.exe
if not exist "%JAVA_EXE%" set JAVA_EXE=java
"%JAVA_EXE%" -cp "%CP%" network.crypta.launcher.LauncherKt %*
