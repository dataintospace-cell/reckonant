"""Runs the Reckonant API and the front end page on the same address.

Save this file in the reckonant_backend folder (next to api.py), and keep index.html in
D:\Reckonant\reckonant-frontend. Then run:
    python -m uvicorn serve_ui:app --port 8000
and open http://localhost:8000/
"""
import os
from fastapi.staticfiles import StaticFiles

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND = r"D:\Reckonant\reckonant-frontend"

from api import app  # the data API, unchanged

app.mount("/", StaticFiles(directory=FRONTEND, html=True), name="ui")
