# Portfolio title
SyncHarbor — Reliable REST Data Imports with Review & Rollback

# Short description
A working API integration tool that collects paginated records, maps source fields, previews changes and saves approved updates to SQLite with recovery snapshots.

# Full description
When your team moves data between systems, the difficult part is often knowing what an import will change—and how to undo it if something is wrong.

I built SyncHarbor to make that process visible and controlled. It connects to a configurable REST endpoint, follows pagination, maps nested fields, and compares incoming records with a persistent local dataset. Users can review additions and updates before committing. Conflicting duplicate IDs or incomplete mappings block the import, and existing-value updates require explicit approval.

The implementation also protects against stale reviews, keeps recovery snapshots, records import activity, and exports the resulting dataset as CSV or JSON.

This is a new independent working project. Its included fictional source data is fetched over real local HTTP, and nine automated tests verified the implemented workflow, recovery behavior and error handling. Screenshots show the running application; no client outcome or public deployment is implied.

Relevant skills: REST APIs · Python · SQLite · JSON field mapping · Data synchronization · Integration troubleshooting · Frontend/backend communication.

# Suggested image order
1. Workspace and source mapping.
2. Revised-source review with saved data and recovery points.
