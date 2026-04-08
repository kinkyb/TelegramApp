#!/bin/bash
# start_bot.command — Start watcher + bot for KinkyBeatrice Lounge
# Double-click this file in Finder to launch everything.

cd "$(dirname "$0")"

source venv/bin/activate

echo "🤖 Starting watcher and bot..."
echo ""

# Start watcher in background
python watcher.py &
WATCHER_PID=$!
echo "👁  Watcher started (PID $WATCHER_PID)"

# Start bot in foreground (keeps the terminal window open)
echo "📡 Bot starting..."
echo ""
python bot.py

# If bot exits, kill watcher too
kill $WATCHER_PID 2>/dev/null
echo "👋 Both processes stopped."
