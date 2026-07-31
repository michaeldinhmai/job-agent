"""Launcher so the dashboard can be started regardless of working directory."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jobagent.webapp import main

if __name__ == "__main__":
    main()
