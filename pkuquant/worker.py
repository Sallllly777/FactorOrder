"""Isolated research job subprocess. Input is produced only by the local service."""
import json
from pathlib import Path
import sys


def main():
    local=Path(__file__).resolve().parents[1]/'.analysis_deps'
    if local.is_dir(): sys.path.insert(0,str(local))
    from .api import Research
    from .config import Settings
    spec=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
    lab=Research(spec['source']['data'],spec['source'].get('states'),spec['source'].get('security_history'),reference_cache=spec['source'].get('reference_cache'))
    lab.run(spec['alpha'],Settings(**spec['settings'])).save(spec['output'])


if __name__=='__main__': main()
