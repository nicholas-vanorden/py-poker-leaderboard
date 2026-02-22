# py-poker-leaderboard

Serverless poker leaderboard built with AWS Lambda, DynamoDB, and Python

A single AWS Lambda function that:

- Serves an HTML leaderboard page for multiple poker tournament series.
- Loads player standings from DynamoDB.
- Provides a password-gated "Add Results" dialog in the UI.
- Accepts `POST /results` to persist new result rows back to DynamoDB.

## What It Does

### `GET /` (or Lambda root route)

Returns an HTML page with:

- `h1` tournament header
- Series dropdown (distinct `series` values ordered by latest `updated` in each series)
- Optional `?series=<name>` query support to preselect a series on load (case-insensitive match)
- "Updated <date>" for the currently selected series
- Leaderboard table filtered by selected series
- Tie-aware rank display (`T2nd`, etc.) calculated per series

### `POST /results`

Accepts a JSON array of result rows and upserts players in DynamoDB.

Example payload:

```json
[
  { "place": "1st", "name": "Alice", "series": "Winter 2026", "points": 10 },
  { "place": "2nd", "name": "Bob", "series": "Winter 2026", "points": 7 }
]
```

Rules:

- Player matching is case-insensitive by `name` and `series`.
- A player can appear only once per save payload per `name + series`.
- `series` is required on each result row.
- `points` must be a whole number greater than 0.

If player exists:

- `points = current_points + new_points`
- `results = place != "None" ? current_results + "," + place : current_results`
- `updated = now (ISO 8601)`

If player does not exist:

- `id = uuid4 string`
- `name = submitted name`
- `series = submitted series`
- `points = submitted points`
- `results = place != "None" ? place : ""`
- `updated = now (ISO 8601)`

## DynamoDB Table

Table name (default): `FnsPokerPlayers`

Attributes used:

- `id` (String) - partition key
- `name` (String)
- `series` (String)
- `points` (Number)
- `results` (String)
- `updated` (String, ISO 8601)

The Lambda reads table name from env var:

- `TABLE_NAME` (optional)
- Defaults to `FnsPokerPlayers`

## Frontend Behavior

The rendered page includes:

- Series selector above the table
- Initial load defaults to the first (most recently updated) series
- "Updated ..." changes when series selection changes
- Bottom-right "Add Results" link
- Password prompt
- Dialog with one or more result rows:
  - Header series controls ("Add Results to:")
  - Existing series dropdown defaulting to current page series
  - `New`/`Choose` toggle to switch between existing-series dropdown and a new-series text input
  - New-series mode validates: non-empty and must not already exist (case-insensitive)
  - `Place` dropdown (`1st`..`9th`, `Bubble`, `None`)
  - `Player` searchable free-text input (datalist from existing players)
  - `Points` integer-only input
- Save posts to `/results` and then navigates back showing the series that was just saved/created
- Mobile behavior: on small screens, "Updated ..." is shown below the series selector and left-aligned

## AWS Setup

1. Deploy `py-poker-leaderboard/index.py` as Lambda handler `index.handler`.
2. Use Python runtime (for example `python3.12`).
3. Configure API Gateway routes:
   - `GET /` -> this Lambda
   - `POST /results` -> this Lambda
4. Set Lambda environment variable (if needed):
   - `TABLE_NAME=FnsPokerPlayers`
5. Ensure Lambda IAM permissions include:
   - `dynamodb:Scan`
   - `dynamodb:UpdateItem`
   - `dynamodb:PutItem`
   on the target table.
6. Recommended Lambda settings for larger payload saves:
   - Timeout >= 15 seconds
   - Memory >= 256 MB

## Logging / Troubleshooting

The Lambda emits CloudWatch logs for:

- Request method/path
- Request body size
- Table scan count
- Per-row processing/update/create actions
- Validation and exception paths

If you see `Status: timeout` in CloudWatch report logs, increase Lambda timeout and memory.

## Notes

- `py-poker-leaderboard/index.py` is the Lambda implementation.
- Password auth is temporary and client-side only; replace with real auth for production.
