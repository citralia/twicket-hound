#!/bin/bash
# Check if tmux is installed
if ! command -v tmux &> /dev/null; then
    echo "Error: tmux is not installed. Install it with 'brew install tmux'."
    exit 1
fi

# Check if venv exists and activate it (replace with your venv path)
VENV_PATH="./venv/bin/activate"
if [ ! -f "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH."
    exit 1
fi
source "$VENV_PATH"

# Check if python3 is available in venv
if ! command -v python3 &> /dev/null; then
    echo "Error: python3 not found in virtual environment."
    exit 1
fi

# Check if twickets_scraper.py exists
if [ ! -f "twickets.py" ]; then
    echo "Error: twickets.py not found in current directory."
    exit 1
fi

# Start a new tmux session with logging
tmux new-session -d -s twickets 'python3 twickets.py 2> tmux.log'
if [ $? -eq 0 ]; then
    echo "Twickets scraper started in tmux session 'twickets' using venv. To attach, run: tmux attach -t twickets"
    echo "Errors (if any) will be logged to tmux.log"
else
    echo "Error: Failed to start tmux session."
    exit 1
fi