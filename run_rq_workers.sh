#!/usr/bin/env bash
# Start RQ workers separately from the web server

# Activate venv if needed
# source venv/bin/activate

# Adjust "high" "default" to your queues
rq worker -u redis://localhost:6379/0 high default