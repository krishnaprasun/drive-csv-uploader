#!/bin/bash
# Launch the Drive CSV Uploader UI in your browser.
cd "$(dirname "$0")"
exec ./venv/bin/streamlit run app.py "$@"
