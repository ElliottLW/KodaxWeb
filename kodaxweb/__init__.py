"""KodaxWeb: local web app for Kodak Smart Home cameras."""
from pathlib import Path

# Your files (cameras.json, public.pem): git-ignored, kept out of the code.
DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
