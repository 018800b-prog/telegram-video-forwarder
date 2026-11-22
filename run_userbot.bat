@echo off
title Telegram Userbot Forwarder
color 0E
echo ========================================================
echo       Starting Telegram Userbot Forwarder
echo ========================================================
echo.

if not exist .env (
    echo [!] .env file not found! Copying from .env.example...
    copy .env.example .env
    echo [!] Please edit the .env file with your credentials and run again.
    pause
    exit /b
)

py -m pip install -r requirements.txt
cls
echo Starting userbot...
py userbot.py
pause
