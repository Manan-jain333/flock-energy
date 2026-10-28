"""Regenerate openapi.json from the app:  python -m scripts.export_openapi"""
import json
from pathlib import Path

from app.main import app

Path(__file__).resolve().parent.parent.joinpath("openapi.json").write_text(
    json.dumps(app.openapi(), indent=2) + "\n", encoding="utf8"
)
print("wrote openapi.json")
