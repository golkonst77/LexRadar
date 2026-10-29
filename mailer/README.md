# Mailer snapshot

Python 3.9+; standard library only, system timezone database required.

```sh
cp config.example.json config.json
cp batch.example.json batch.json
cp approved.example.json approved.json
python3 -m unittest -v test_mailer
```

Replace the fictional sender and API URL in config.json AND the matching
validation constants in mailer_v2.py before local use. Replace example batch IDs
with locally reviewed CRM IDs. mailer.py is the older read-only preview;
its IDS list contains only a fictional example.

Load ESPO_API_KEY from your private environment. Never commit it.
`python3 mailer_v2.py preview` (or `status`) performs GET requests and writes a
local journal. No CRM requests were made while preparing this snapshot.

Scheduling requires enabled=true, dry_run=false, explicitly reviewed IDs in
approved.json and `python3 mailer_v2.py schedule --confirm-schedule`.
Do not enable it before reviewing recipient, PDF content and permission to send.
The local lock does not protect against another host or manual CRM changes.
API pagination and field compatibility must be verified locally first.

Original deployment notes and verification logs were excluded because they
describe a real installation. Defaults here are disabled and dry-run only.
