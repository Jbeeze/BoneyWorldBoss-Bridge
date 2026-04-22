#!/bin/bash
# Boney World Bosses - Bridge Launcher (macOS)
# Double-click this file to start the bridge

# Change to the directory where this script is located
cd "$(dirname "$0")"

echo "========================================"
echo "  Boney World Bosses - Bridge"
echo "========================================"
echo ""

# Check if Python 3 is installed
if ! command -v python3 &> /dev/null; then
    echo "ERROR: Python 3 is not installed."
    echo "Please install Python 3 from https://www.python.org/downloads/"
    echo ""
    read -p "Press Enter to exit..."
    exit 1
fi

# Check if requests module is installed
if ! python3 -c "import requests" &> /dev/null; then
    echo "Installing required packages..."
    pip3 install requests
    echo ""
fi

# Run the bridge. Configuration is read from the addon's SavedVariables -
# run /bwb setup in WoW if the bridge reports that config is missing.
python3 bridge.py

# Keep window open if bridge exits
echo ""
read -p "Press Enter to exit..."
