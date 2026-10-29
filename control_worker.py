"""Fixed offline checks used by Control Center; never invoke production pipelines."""
import argparse
import json
from pathlib import Path

from control_catalog import Catalog
from control_leads import load_leads


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('inspect-import', 'offline-results'))
    parser.add_argument('--parser-dir', type=Path, required=True)
    parser.add_argument('--results-dir', type=Path, required=True)
    args = parser.parse_args()
    if args.kind == 'inspect-import':
        records, warnings = load_leads(args.parser_dir)
        result = {'leads': len(records), 'warnings': warnings,
                  'usable_contacts': sum(bool(r['websites'] and (r['phone'] or r['email'])) for r in records)}
    else:
        data = Catalog(args.results_dir, args.parser_dir).snapshot()
        warnings = data['warnings']
        result = {'audits': sum(r['kind'] == 'audit' for r in data['reports']),
                  'pdf_files': sum(r['kind'] == 'pdf' for r in data['reports']), 'warnings': warnings}
    print(json.dumps(result, ensure_ascii=False))
    return int(bool(warnings))


if __name__ == '__main__':
    raise SystemExit(main())
