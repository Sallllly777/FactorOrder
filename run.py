"""Convenient local runner; installed package also exposes `pkuquant`."""
from pathlib import Path
import sys

local_deps = Path(__file__).resolve().parent / '.analysis_deps'
if local_deps.is_dir(): sys.path.insert(0,str(local_deps))
if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(encoding='utf-8',errors='replace')
from pkuquant.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
