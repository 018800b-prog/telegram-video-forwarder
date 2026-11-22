@echo off
title Telegram Video Forwarder Bot
color 0A
echo ========================================================
echo       Starting Telegram Video Forwarder Bot
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
echo Starting bot...
py bot.py
pause
