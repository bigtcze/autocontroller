#!/usr/bin/env python3
"""Guest-side helper for the Engram rollback scenario: rewrite only the `version`
field of memory/active.json, atomically. Usage: set_active_version.py PATH VERSION
Stdlib only; never touches the generation pointer.
"""
import json
import os
import sys
from pathlib import Path

path = Path(sys.argv[1])
version = sys.argv[2]
data = json.loads(path.read_text(encoding='utf-8'))
if 'generation' not in data:
    raise SystemExit('active.json has no generation')
data['version'] = version
tmp = path.with_suffix('.tmp')
tmp.write_text(json.dumps(data), encoding='utf-8')
os.replace(tmp, path)
print(json.dumps(data, sort_keys=True))
