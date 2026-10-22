"""Portable runtime paths and a process-wide gate for expensive enumeration."""
import os
from functools import wraps
from pathlib import Path
from threading import Lock

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get('LOTTO_LAB_DATA_DIR', ROOT / 'data')).expanduser().resolve()
_analysis_lock = Lock()


def serialized_analysis(function):
    @wraps(function)
    def run(*args, **kwargs):
        # Queue requests rather than allocating several analysis working sets.
        with _analysis_lock:
            return function(*args, **kwargs)
    return run
