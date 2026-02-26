# AGENTS.md

## Purpose
This repository contains a Python AWS Lambda that serves a poker leaderboard HTML page and accepts result submissions to update DynamoDB.

## Scope
- Primary runtime file: `poker-leaderboard/index.py`
- Deployment workflow: `.github/workflows/deploy.yaml`
- Documentation: `README.md`

## Runtime Behavior
- `GET /` returns HTML leaderboard.
- `POST /results` accepts a JSON array of result rows and upserts players in DynamoDB.
- DynamoDB table defaults to `FnsPokerPlayers` (override with `TABLE_NAME` env var).

## Data Rules
- Match existing players by `name` case-insensitively AND `series` case-insensitively.
- Reject payloads where the same `name + series` appears more than once.
- `series` is required on each POST row.
- `points` must be whole number `> 0`.
- Existing player update:
  - `points += submitted points`
  - append `place` to `results` unless `place == "None"`
  - `updated = current ISO 8601 UTC timestamp`
- New player insert:
  - `id = uuid4`
  - `name`, `series`, `points` from payload
  - `results = place` unless `place == "None"` (then empty string)
  - `updated = current ISO 8601 UTC timestamp`

## Frontend Notes
- HTML is rendered from Python string templates in `index.py`.
- Leaderboard is client-filtered by a series dropdown populated from distinct table series values, ordered by latest series `updated` desc.
- Leaderboard supports `?series=<name>` query to preselect a series on load (case-insensitive).
- Leaderboard rank labels are computed per series.
- "Updated ..." text reflects the selected series; on small screens it appears below the series dropdown, left-aligned.
- "Add Results" UI posts to `/results`.
- Add Results dialog has series controls:
  - existing-series dropdown defaulting to the currently selected main-view series
  - `New`/`Choose` toggle that switches to a new-series text input
  - new-series validation requires non-empty value and rejects existing series names case-insensitively
- After successful save, client navigates back with `?series=<saved series>` so the updated/created series is shown.
- "Export" link opens export dialog with:
  - format radio options (`CSV`, `JSON`)
  - series checkboxes (one per series; one to many/all selectable)
  - `Cancel` closes dialog
  - `Export` downloads and closes dialog
- Export output excludes `id` and is sorted by `series`, then `points` descending, then `name`.
- Points input is restricted to integer-only entry.
- Client-side password prompt is temporary and not secure auth.

## Deployment Notes
- GitHub Actions deploys on merged PRs to `main`.
- Workflow zips `poker-leaderboard/*` and updates Lambda function `simple-html`.
- Pre-deploy workflow check ensures `poker-leaderboard/index.py` exists.

## Editing Guardrails
- Keep changes minimal and consistent with current style.
- Do not remove logging; CloudWatch logs are used for debugging production issues.
- Preserve API route compatibility (`GET /`, `POST /results`) unless explicitly asked.
- If changing payload shape, update both frontend JS and backend parsing/validation.
- If changing table schema assumptions, update `README.md` in the same change.

## Validation Checklist (before merge)
1. Python syntax check passes for `poker-leaderboard/index.py`.
2. `POST /results` validates duplicates by `name + series`, requires `series`, and enforces integer points.
3. Leaderboard rendering ranks by points descending and handles ties per series.
4. Add Results dialog validates new series names are non-empty and unique vs existing series (case-insensitive).
5. After save, UI returns showing the saved series.
6. Export dialog supports CSV/JSON and series multi-select, with cancel/export button behavior intact.
7. Export rows are ordered by series, points desc, name and include all non-`id` columns.
8. Deployment workflow still targets Lambda `simple-html`.
