# SyncHarbor
## REST synchronization with a review and recovery workflow

**Category:** API & System Integration  
**Author:** Ismail Habib  
**Project type:** New independent working project  
**Stack:** Python, HTTP/JSON, SQLite, HTML, CSS, JavaScript

### The problem

A team can have an accessible data API and still depend on manual exports, spreadsheet comparisons and risky bulk imports. Pagination complicates collection; source fields rarely match destination fields; and replacing existing values without a clear review creates avoidable uncertainty.

### The delivered solution

SyncHarbor fetches a paginated source over HTTP, maps its values to a consistent local dataset, and displays exactly which records would be added or changed. A preview does not write records. Existing-value updates require explicit approval, and the reviewed database revision must still be current when the user commits.

Imports and restores use SQLite transactions. Recovery snapshots are saved before changes; restoring a snapshot also preserves the state it replaces. CSV and JSON exports make the reconciled dataset usable outside the application.

### An implemented end-to-end workflow

A configurable URL connects to a real HTTP endpoint. The application follows `next` links, rejects loops or cross-origin pagination, maps nested source fields and detects malformed or conflicting records. The browser presents additions, updates and rejected rows alongside the saved dataset. The user commits reviewed changes or adjusts the mapping and tries again.

The included verification source has two editions. Edition 1 spans two pages and contains six fictional records. Edition 2 adds one record and changes two existing records. Those actual observed counts exercise the review and approval behavior; they are not business-performance claims.

### What was verified

Nine automated tests passed locally. Tests exercise actual loopback HTTP requests, two-page collection, preview without database writes, commit and CSV/JSON exports, explicit update approval, stale-preview rejection, snapshot restoration, duplicate-conflict blocking, pagination-loop detection, remote-fetch opt-in, Host/Origin checks, formula-safe CSV cells and retention of records absent from a source response. The fixtures and test databases are isolated from saved user data.

### Why the implementation matters

The work demonstrates API integration beyond a successful request: handling pagination, interpreting external data, comparing it with existing state, presenting meaningful decisions, and recovering from mistakes. The user can inspect and reverse a change without requiring a complete system replacement.

### Honest scope

This project was independently built and verified locally. No past client delivery, production deployment, revenue improvement, time-saving percentage or live commercial dataset is claimed. It is a single-user loopback application with a documented JSON envelope; authentication adapters, scheduled processing and shared hosting would be separate work.

### Screenshot captions

1. **Review before write:** The actual application reads a two-page verification endpoint and presents proposed records alongside the mapping and recovery controls.
2. **Changes with context:** A revised source reveals one addition and two changes against the saved dataset; explicit approval protects existing values.
