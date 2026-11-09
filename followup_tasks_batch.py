"""Create call tasks only for explicitly selected Email IDs."""
import argparse
import sys

from followup_tasks import ROOT, LEGACY, run
from src.lexradar import espo_export as espo


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--email', action='append', required=True, help='Явно выбранный Email ID; до 10')
    args = parser.parse_args()
    try:
        results = run(ROOT / '.control-center-state', LEGACY, args.email, apply=True)
    except (espo.Hold, OSError, ValueError, KeyError) as exc:
        print('STOP: ' + str(exc), file=sys.stderr)
        return 1
    return int(any(r['status'] == 'ERROR' for r in results))


if __name__ == '__main__':
    sys.exit(main())
