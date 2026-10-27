"""Offline migration CLI and importable entry point."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from data_foundation.legacy import import_legacy

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',required=True)
    parser.add_argument('--dry-run',action='store_true')
    args=parser.parse_args()
    print(json.dumps(import_legacy(args.db,dry_run=args.dry_run),indent=2,sort_keys=True))
