"""Local Parser v3 handoff. No network, CRM writes, LLM calls or mail."""

import argparse
import csv
import fcntl
import hashlib
import io
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


REQUIRED = {
    "Организация", "Официальный сайт", "Email", "Телефон", "Бренд", "Филиал",
    "Официальный сайт бренда подтверждён", "Исходная карточка",
}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def convert(raw):
    reader = csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";", strict=True)
    headers = reader.fieldnames or []
    if not REQUIRED <= set(headers) or len(headers) != len(set(headers)):
        raise ValueError("Missing or duplicate Parser headers")
    audit, candidates, seen = [], [], set()
    for row in reader:
        if None in row or any(value is None for value in row.values()):
            raise ValueError("Parser column count mismatch")
        company = row["Организация"].strip()
        url = urlsplit(row["Официальный сайт"].strip())
        if (not company or url.scheme not in {"http", "https"} or not url.hostname
                or url.username or url.password or any(c.isspace() for c in url.geturl())):
            raise ValueError("Invalid organization or HTTP(S) website")
        # Access control and public-address validation remain in the existing collector.
        website = urlunsplit((url.scheme.lower(), url.netloc.lower(), url.path or "/",
                              url.query, ""))
        identity = json.dumps([company, website], ensure_ascii=False).encode()
        stable_id = "parser-" + digest(identity)
        if stable_id in seen:
            raise ValueError("Duplicate organization/website; resolve Parser rows first")
        seen.add(stable_id)
        # Parser brand confirmation alone does not verify the legal operator.
        audit.append({"url": website, "company_name": f"Сайт {url.hostname} "
                      "(связь с организацией не подтверждена)", "email": ""})
        candidates.append({
            "id": stable_id, "company_name": company, "website": website,
            "email": row["Email"].strip(), "phone": row["Телефон"].strip(),
            "source_card": row["Исходная карточка"].strip(),
            "parser_brand": row["Бренд"], "parser_branch": row["Филиал"],
            "parser_brand_confirmation": row["Официальный сайт бренда подтверждён"],
            "identity_verified": False, "recipient_verified": False,
            "export_approved": False, "automatic_send_allowed": False,
            "crm_status": "NOT_EXPORTED", "pdf_path": "",
            "hold_reasons": ["identity_review_required", "audit_and_pdf_review_required",
                             "crm_write_requires_separate_approval"]
                            + (["missing_email"] if not row["Email"].strip()
                               else ["recipient_review_required"]),
        })
    if not audit:
        raise ValueError("Empty Parser CSV")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=["url", "company_name", "email"])
    writer.writeheader()
    writer.writerows(audit)
    return stream.getvalue().encode(), candidates


def prepare(csv_path, sqlite_path, data_root):
    data_root = data_root.absolute()
    repository = Path(__file__).resolve().parents[2]
    if data_root.resolve().is_relative_to(repository):
        raise ValueError("Data directory must be outside the Git checkout")
    if any(p.is_symlink() for p in (data_root, *data_root.parents)):
        raise ValueError("Symlink data directory refused")
    raw = csv_path.read_bytes()
    audit, candidates = convert(raw)
    run_id = "parser-" + digest(raw)
    data_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if data_root.stat().st_mode & 0o077:
        raise ValueError("Data directory must have private permissions (0700)")
    with (data_root / ".parser-import.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix=".parser-", dir=data_root) as temporary:
            staging = Path(temporary) / "handoff"
            staging.mkdir(mode=0o700)
            # SQLite backup includes committed WAL data without touching Parser's database.
            with sqlite3.connect(sqlite_path.resolve().as_uri() + "?mode=ro", uri=True) as source:
                with sqlite3.connect(staging / "parser.sqlite") as destination:
                    source.backup(destination)
                    if destination.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                        raise ValueError("SQLite integrity check failed")
            files = {
                "parser.csv": raw, "lexradar.csv": audit,
                "crm-candidates.json": (json.dumps(candidates, ensure_ascii=False, indent=2)
                                        + "\n").encode(),
                "parser.sqlite": (staging / "parser.sqlite").read_bytes(),
            }
            manifest = {
                "schema": "parser-handoff-1", "run_id": run_id,
                "rows": len(candidates), "crm_status": "NOT_EXPORTED",
                "automatic_send_allowed": False,
                "parser_screening_is_legal_audit": False,
                "sha256": {name: digest(data) for name, data in files.items()},
            }
            files["import.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
            target = data_root / run_id
            if target.is_symlink():
                raise ValueError("Symlink run directory refused")
            if target.exists():
                for name, data in files.items():
                    if (target / name).is_symlink() or (target / name).read_bytes() != data:
                        raise ValueError("Existing run differs; inspect saved inputs, do not overwrite")
                return target, len(candidates), True
            for name, data in files.items():
                (staging / name).write_bytes(data)
                (staging / name).chmod(0o600)
            # Publish only a complete handoff; interrupted staging is never a successful run.
            staging.rename(target)
            return target, len(candidates), False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", type=Path)
    parser.add_argument("--sqlite", type=Path, required=True)
    parser.add_argument("--data-root", type=Path,
                        default=Path("/home/golkonst/lexradar-data"))
    parser.add_argument("--dry-run", action="store_true",
                        help="Local handoff only (also the default); never calls CRM or the site")
    args = parser.parse_args(argv)
    previous = os.umask(0o077)
    try:
        target, rows, reused = prepare(args.csv, args.sqlite, args.data_root)
    except (OSError, ValueError, csv.Error, sqlite3.Error):
        parser.exit(1, "Import HOLD: input/storage validation failed; no CRM operation attempted\n")
    finally:
        os.umask(previous)
    print(json.dumps({"run": str(target), "rows": rows, "reused": reused,
                      "crm_status": "NOT_EXPORTED", "automatic_send_allowed": False}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
