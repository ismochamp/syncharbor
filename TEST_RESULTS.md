# Verification results — SyncHarbor

- **Run date:** 28 September 2026.
- **Environment:** macOS; Python 3.14; Python standard library only.
- **Command:** `python3 -m unittest -v test_app.py`.
- **Result:** **9 tests passed** in the captured final run.
- **Evidence:** `test-run.txt` contains the actual test output.
- **Isolation:** temporary SQLite files and a temporary loopback HTTP server; no remote endpoint or user credentials were used.

| Test | Behavior verified |
|---|---|
| Real HTTP import and exports | Fetches two JSON pages, proposes six additions, leaves DB unchanged at preview, commits six records, downloads CSV/JSON |
| Explicit update approval | Revised source has one addition and two updates; missing approval blocks commit |
| Stale preview protection | Dataset changes invalidate earlier previews |
| Reversible snapshots | Restore to empty and back to imported data; stale restore revision rejected |
| Conflicting inputs | Duplicate IDs with differing content and missing mapping paths block commit |
| Pagination loop detection | Repeated next URL rejects the fetch |
| Request boundaries | Remote fetch requires opt-in; invalid Host/Origin rejected |
| Spreadsheet-safe exports | Formula-like source values are neutralized in CSV |
| Non-destructive import | Empty source does not delete saved records |

