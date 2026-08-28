#!/bin/bash
# run-tests.sh - Run pytest with virtualenv initialization
# shellcheck disable=SC1091
# Usage: ./scripts/run-tests.sh [pytest arguments]
# Examples:
#   ./scripts/run-tests.sh                    # Run all tests
#   ./scripts/run-tests.sh -v                 # Run all tests with verbose output
#   ./scripts/run-tests.sh tests/test_server.py  # Run specific tests
#   ./scripts/run-tests.sh -k test_query     # Run tests matching pattern

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Virtual environment path
VENV_PATH="$HOME/.virtualenvs/forgejo-to-github"

# Check if virtualenv exists
if [ ! -d "$VENV_PATH" ]; then
    echo "Error: Virtual environment not found at $VENV_PATH"
    echo "Please create it first with:"
    echo "  python3 -m venv $VENV_PATH"
    echo "  source $VENV_PATH/bin/activate"
    echo "  pip install -e '.[dev]'"
    exit 1
fi

# Activate virtualenv
source "$VENV_PATH/bin/activate"

# Change to project root
cd "$SCRIPT_DIR/.."

# Run pytest with all arguments passed through
exec pytest "$@"
