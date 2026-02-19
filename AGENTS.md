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
- Leaderboard rank labels are computed per series.
- "Updated ..." text reflects the selected series; on small screens it appears below the series dropdown, left-aligned.
- "Add Results" UI posts to `/results`.
- Add Results dialog has its own series dropdown that defaults to the series selected in the main view.
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
4. Deployment workflow still targets Lambda `simple-html`.
