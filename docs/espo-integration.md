# Espo export: source snapshot

The snapshot uses fictional API, sender, Email, Account and Document constants.
Configure `src/lexradar/espo_export.py` for your private installation before use.
Do not put secrets, client IDs, manifests, journals or client PDFs in Git.
`ESPO_API_KEY` is read from the environment or the private
`~/.config/lexradar/espo.env` file. No key is distributed here.

Run `./espo-export --help` from this checkout, without installing the package.
The JSON/CSV manifest examples in `examples/espo-export/` use fictional data.
Put your locally reviewed PDF alongside your private manifest; every row needs
explicit recipient verification and export approval. Preview validates local
inputs and writes a local journal; it does not call the CRM:

```sh
./espo-export /private/path/manifest.json --journal /private/path/export-state.json
```

`--check-connection PRIVATE_OUTPUT_DIR` reads the configured sample records,
attachments and schema and writes a private snapshot. Do not commit its output.
An approved local template has `name`, `body`, and `isHtml` fields. Draft creation
requires a separate explicit `--confirm-create-drafts` flag and a reviewed
`--template` file. It creates CRM records; it must be authorized separately.
The exporter never sends or schedules mail. Keep its journal for recovery and
resolve uncertain writes manually instead of deleting the journal and retrying.

Offline tests use a fake API: `PYTHONPATH=src python3 -m unittest discover -s tests -p test_espo_export.py -v`.
