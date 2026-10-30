"""Invoke the existing real rules engine, without paid transport or CRM."""
import json
import sys
from pathlib import Path

from control_analysis import ENGINE


def main():
    sys.path.insert(0, str(ENGINE / 'src'))
    from lexradar.mvp_batch import run
    state = run(Path(sys.argv[1]), Path(sys.argv[2]), mode='prepare', max_sites=1,
                max_ai_calls=0, allow_site_collection=True, ignore_robots=False)
    row = state['rows'][0]
    print(json.dumps({'status': row['status'], 'reason': row.get('reason'),
                      'stages': row.get('stages', [])}, ensure_ascii=False), flush=True)
    return 0 if 'evidence_packet:SUCCESS' in row.get('stages', []) and row['status'] != 'ERROR' else 1


if __name__ == '__main__':
    raise SystemExit(main())
