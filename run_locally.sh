#!/bin/sh
pip install -r requirements.txt
uvicorn backend.app:app --reload --port 8000
