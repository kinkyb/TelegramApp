#!/bin/bash
# Render: Flask API only.
# The Telegram bot runs locally on the creator's Mac (avoids dual-polling conflict).

gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT
