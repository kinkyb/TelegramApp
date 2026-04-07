#!/bin/bash
# Start both the Flask API and the Telegram bot in the same process group.
# gunicorn serves the web traffic; the bot runs as a background process.

python bot.py &
gunicorn app:app --workers 2 --bind 0.0.0.0:$PORT
