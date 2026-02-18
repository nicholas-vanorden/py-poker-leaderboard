import base64
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from uuid import uuid4

import boto3
from botocore.exceptions import BotoCoreError, ClientError

TABLE_NAME = os.getenv("TABLE_NAME", "FnsPokerPlayers")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def _ordinal(value):
    if 10 <= value % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(value % 10, "th")
    return f"{value}{suffix}"


def _parse_points(value):
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _format_points(value):
    if value == value.to_integral_value():
        return str(int(value))
    normalized = format(value.normalize(), "f")
    return normalized.rstrip("0").rstrip(".")


def _parse_iso_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _scan_table_items(table):
    items = []
    response = table.scan()
    items.extend(response.get("Items", []))
    while "LastEvaluatedKey" in response:
        response = table.scan(ExclusiveStartKey=response["LastEvaluatedKey"])
        items.extend(response.get("Items", []))
    logger.info("Scanned DynamoDB table '%s' and loaded %d item(s).", table.name, len(items))
    return items


def _load_players(table_name):
    table = boto3.resource("dynamodb").Table(table_name)
    items = _scan_table_items(table)

    players = []
    for item in items:
        players.append(
            {
                "id": str(item.get("id", "")),
                "name": str(item.get("name", "")),
                "points": _parse_points(item.get("points", 0)),
                "results": str(item.get("results", "")),
                "updated": str(item.get("updated", "")),
            }
        )

    players.sort(key=lambda player: (-player["points"], player["name"].lower(), player["name"]))
    return players


def _now_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_results_text(current_results, new_place):
    current = str(current_results or "").strip()
    if new_place == "None":
        return current
    if not current:
        return new_place
    return f"{current},{new_place}"


def _upsert_results(table_name, submitted_results):
    if not isinstance(submitted_results, list) or not submitted_results:
        raise ValueError("Request body must be a non-empty array of results.")

    logger.info("Starting results upsert for %d row(s) into table '%s'.", len(submitted_results), table_name)
    table = boto3.resource("dynamodb").Table(table_name)
    players = _load_players(table_name)
    player_lookup = {player["name"].casefold(): player for player in players if player["name"].strip()}
    now_iso = _now_iso()
    processed = 0
    request_names = set()

    for index, new_result in enumerate(submitted_results, start=1):
        place = str(new_result.get("place", "None")).strip() or "None"
        name = str(new_result.get("name", new_result.get("player", ""))).strip()
        points = _parse_points(new_result.get("points", 0))
        logger.info("Processing row %d: player='%s', place='%s', points='%s'.", index, name, place, str(points))

        if not name:
            raise ValueError("Each result row must include a player name.")
        if points <= 0 or points != points.to_integral_value():
            raise ValueError("Each result row must include whole-number points greater than 0.")

        lookup_name = name.casefold()
        if lookup_name in request_names:
            raise ValueError("A player can only appear once in a single save.")
        request_names.add(lookup_name)

        lookup_key = lookup_name
        existing_player = player_lookup.get(lookup_key)

        if existing_player:
            next_points = existing_player["points"] + points
            next_results = _normalize_results_text(existing_player.get("results", ""), place)
            logger.info(
                "Updating existing player id='%s' name='%s' -> points='%s', results='%s'.",
                existing_player["id"],
                existing_player["name"],
                str(next_points),
                next_results,
            )
            table.update_item(
                Key={"id": existing_player["id"]},
                UpdateExpression="SET #points = :points, #results = :results, #updated = :updated",
                ExpressionAttributeNames={
                    "#points": "points",
                    "#results": "results",
                    "#updated": "updated",
                },
                ExpressionAttributeValues={
                    ":points": next_points,
                    ":results": next_results,
                    ":updated": now_iso,
                },
            )
            existing_player["points"] = next_points
            existing_player["results"] = next_results
            existing_player["updated"] = now_iso
        else:
            player_id = str(uuid4())
            initial_results = "" if place == "None" else place
            new_player = {
                "id": player_id,
                "name": name,
                "points": points,
                "results": initial_results,
                "updated": now_iso,
            }
            logger.info(
                "Creating new player id='%s' name='%s' with points='%s' and results='%s'.",
                new_player["id"],
                new_player["name"],
                str(new_player["points"]),
                new_player["results"],
            )
            table.put_item(Item=new_player)
            player_lookup[lookup_key] = new_player

        processed += 1

    logger.info("Results upsert complete. Processed %d row(s).", processed)
    return {"processed": processed}


def _json_response(status_code, payload):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload),
    }


def _html_response(status_code, body):
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "text/html"},
        "body": body,
    }


def _http_method(event):
    request_context = event.get("requestContext", {})
    http_context = request_context.get("http", {})
    method = http_context.get("method") or event.get("httpMethod") or "GET"
    return str(method).upper()


def _request_path(event):
    return str(event.get("rawPath") or event.get("path") or "/")


def _parse_json_body(event):
    body = event.get("body") or "[]"
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body).decode("utf-8")
    logger.info("Received request body length: %d", len(body))
    return json.loads(body)


def _rank_label(index, points, point_counts):
    label = _ordinal(index)
    if point_counts.get(points, 0) > 1:
        return f"T{label}"
    return label


def _latest_updated_text(players):
    latest = None
    for player in players:
        parsed = _parse_iso_datetime(player["updated"])
        if parsed and (latest is None or parsed > latest):
            latest = parsed

    if latest is None:
        return "Unknown"

    return f"{latest.month}/{latest.day}/{latest.year}"


def _render_rows(players):
    point_counts = {}
    for player in players:
        points = player["points"]
        point_counts[points] = point_counts.get(points, 0) + 1

    rows = []
    display_rank = 0
    previous_points = None
    for index, player in enumerate(players, start=1):
        if previous_points is None or player["points"] != previous_points:
            display_rank = index

        rank = _rank_label(display_rank, player["points"], point_counts)
        previous_points = player["points"]
        rows.append(
            "\n".join(
                [
                    "                <tr>",
                    f"                    <td>{escape(rank)}</td>",
                    f"                    <td>{escape(player['name'])}</td>",
                    f"                    <td>{escape(_format_points(player['points']))}</td>",
                    f"                    <td>{escape(player['results'])}</td>",
                    "                </tr>",
                ]
            )
        )

    return "\n".join(rows)


def _render_player_name_options(players):
    names = sorted(
        {player["name"].strip() for player in players if player["name"].strip()},
        key=lambda value: (value.lower(), value),
    )
    return "\n".join([f"        <option value=\"{escape(name)}\"></option>" for name in names])


def _render_html(players):
    updated_text = _latest_updated_text(players)
    rows_html = _render_rows(players)
    player_name_options_html = _render_player_name_options(players)

    return f"""<!DOCTYPE html>
<html>
    <head>
        <meta charset=\"UTF-8\">
        <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
        <link rel=\"stylesheet\" href=\"https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.classless.min.css\" />
        <title>FNS Poker</title>
    </head>
    <body>
    <div class=\"container\">
        <div>
            <h1>Fire N Slice - Winter Tournament Series</h1>
        </div>
        <div>
            <p>Updated {escape(updated_text)}</p>
        </div>
        <table>
            <thead>
                <tr>
                    <th>Rank</th>
                    <th>Player</th>
                    <th>Points</th>
                    <th>Results</th>
                </tr>
            </thead>
            <tbody>
{rows_html}
            </tbody>
        </table>
        <div class=\"bottom-actions\">
            <a href=\"#\" id=\"add-results-link\">Add Results</a>
        </div>
    </div>
    <dialog id=\"add-results-dialog\">
        <article>
            <header>
                <h3>Add Results</h3>
            </header>
            <table class=\"results-table\">
                <thead>
                    <tr>
                        <th>Place</th>
                        <th>Player</th>
                        <th>Points</th>
                        <th>Remove</th>
                    </tr>
                </thead>
                <tbody id=\"results-rows\"></tbody>
            </table>
            <footer class=\"dialog-actions\">
                <button type=\"button\" id=\"add-row-button\" aria-label=\"Add result row\">+</button>
                <div class=\"dialog-actions-right\">
                    <button type=\"button\" id=\"cancel-results-button\" class=\"secondary\">Cancel</button>
                    <button type=\"button\" id=\"save-results-button\">Save</button>
                </div>
            </footer>
        </article>
    </dialog>
    <datalist id=\"player-name-options\">
{player_name_options_html}
    </datalist>
    <script>
        const PASSWORD = "fn$p@$$w0rd"; // todo
        const placeOptions = ["1st", "2nd", "3rd", "4th", "5th", "6th", "7th", "8th", "9th", "Bubble", "None"];

        const addResultsLink = document.getElementById("add-results-link");
        const addResultsDialog = document.getElementById("add-results-dialog");
        const addRowButton = document.getElementById("add-row-button");
        const cancelResultsButton = document.getElementById("cancel-results-button");
        const saveResultsButton = document.getElementById("save-results-button");
        const resultsRows = document.getElementById("results-rows");

        function getNextPlace(currentPlace) {{
            const currentIndex = placeOptions.indexOf(currentPlace);
            if (currentIndex < 0 || currentIndex === placeOptions.length - 1) {{
                return placeOptions[placeOptions.length - 1];
            }}
            return placeOptions[currentIndex + 1];
        }}

        function createPlaceSelect(defaultPlace) {{
            const select = document.createElement("select");
            select.className = "place-select";
            for (const place of placeOptions) {{
                const option = document.createElement("option");
                option.value = place;
                option.textContent = place;
                if (place === defaultPlace) {{
                    option.selected = true;
                }}
                select.appendChild(option);
            }}
            return select;
        }}

        function createResultRow(defaultPlace) {{
            const row = document.createElement("tr");

            const placeCell = document.createElement("td");
            const playerCell = document.createElement("td");
            const pointsCell = document.createElement("td");
            const actionCell = document.createElement("td");

            const placeSelect = createPlaceSelect(defaultPlace);

            const playerInput = document.createElement("input");
            playerInput.type = "text";
            playerInput.setAttribute("list", "player-name-options");
            playerInput.placeholder = "Player name";
            playerInput.required = true;

            const pointsInput = document.createElement("input");
            pointsInput.type = "text";
            pointsInput.className = "points-input";
            pointsInput.setAttribute("inputmode", "numeric");
            pointsInput.setAttribute("pattern", "[0-9]*");
            pointsInput.placeholder = "Points";
            pointsInput.required = true;
            pointsInput.addEventListener("keydown", (event) => {{
                const allowedKeys = new Set([
                    "Backspace",
                    "Delete",
                    "Tab",
                    "ArrowLeft",
                    "ArrowRight",
                    "Home",
                    "End"
                ]);
                if (allowedKeys.has(event.key)) {{
                    return;
                }}
                if (!/^[0-9]$/.test(event.key)) {{
                    event.preventDefault();
                }}
            }});
            pointsInput.addEventListener("input", () => {{
                pointsInput.value = pointsInput.value.replace(/[^0-9]/g, "");
            }});

            const removeButton = document.createElement("button");
            removeButton.type = "button";
            removeButton.textContent = "x";
            removeButton.className = "secondary remove-row-button";
            removeButton.setAttribute("aria-label", "Remove result row");
            removeButton.addEventListener("click", () => {{
                row.remove();
                if (resultsRows.children.length === 0) {{
                    resultsRows.appendChild(createResultRow(placeOptions[0]));
                }}
            }});

            placeCell.appendChild(placeSelect);
            playerCell.appendChild(playerInput);
            pointsCell.appendChild(pointsInput);
            actionCell.appendChild(removeButton);

            row.appendChild(placeCell);
            row.appendChild(playerCell);
            row.appendChild(pointsCell);
            row.appendChild(actionCell);

            return row;
        }}

        function resetRows() {{
            resultsRows.innerHTML = "";
            resultsRows.appendChild(createResultRow(placeOptions[0]));
        }}

        addRowButton.addEventListener("click", () => {{
            const currentRows = Array.from(resultsRows.querySelectorAll("tr"));
            const lastRow = currentRows[currentRows.length - 1];
            const previousPlace = lastRow ? lastRow.querySelector(".place-select").value : placeOptions[0];
            const defaultPlace = getNextPlace(previousPlace);
            resultsRows.appendChild(createResultRow(defaultPlace));
        }});

        addResultsLink.addEventListener("click", (event) => {{
            event.preventDefault();
            const passwordInputValue = window.prompt("Enter password:");
            if (passwordInputValue === PASSWORD) {{
                resetRows();
                addResultsDialog.showModal();
            }} else if (passwordInputValue !== null) {{
                window.alert("Invalid password.");
            }}
        }});

        cancelResultsButton.addEventListener("click", () => {{
            addResultsDialog.close();
        }});

        saveResultsButton.addEventListener("click", async () => {{
            const rows = Array.from(resultsRows.querySelectorAll("tr"));
            const results = [];
            const seenPlayers = new Set();

            for (const row of rows) {{
                const place = row.querySelector(".place-select").value;
                const playerInput = row.querySelector("input[list='player-name-options']");
                const pointsInput = row.querySelector(".points-input");
                const player = playerInput.value.trim();
                const pointsValue = pointsInput.value;
                const points = Number(pointsValue);

                if (!player) {{
                    window.alert("Each row must include a player.");
                    playerInput.focus();
                    return;
                }}

                if (!pointsValue || Number.isNaN(points) || points <= 0 || !Number.isInteger(points)) {{
                    window.alert("Each row must include whole-number points greater than 0.");
                    pointsInput.focus();
                    return;
                }}

                const normalizedPlayer = player.toLowerCase();
                if (seenPlayers.has(normalizedPlayer)) {{
                    window.alert("A player can only appear once in a single save.");
                    playerInput.focus();
                    return;
                }}
                seenPlayers.add(normalizedPlayer);

                results.push({{
                    place: place,
                    name: player,
                    points: points
                }});
            }}

            const saveEndpoint = `${{window.location.origin}}${{window.location.pathname.replace(/\\/?$/, "/results")}}`;
            saveResultsButton.setAttribute("aria-busy", "true");
            saveResultsButton.disabled = true;
            try {{
                const response = await fetch(saveEndpoint, {{
                    method: "POST",
                    headers: {{
                        "Content-Type": "application/json"
                    }},
                    body: JSON.stringify(results)
                }});

                if (!response.ok) {{
                    let errorMessage = "Failed to save results.";
                    try {{
                        const errorPayload = await response.json();
                        if (errorPayload && errorPayload.error) {{
                            errorMessage = errorPayload.error;
                        }}
                    }} catch (_error) {{
                        // Keep default message when body is not JSON.
                    }}
                    window.alert(errorMessage);
                    return;
                }}

                console.log("Results saved:");
                console.log(results);
                addResultsDialog.close();
                window.location.reload();
            }} catch (_error) {{
                window.alert("Failed to save results.");
            }} finally {{
                saveResultsButton.removeAttribute("aria-busy");
                saveResultsButton.disabled = false;
            }}
        }});
    </script>
    </body>
    <style>
        body {{
            padding: 2rem;
        }}
        .container {{
            max-width: 800px;
            margin: 0 auto;
        }}
        .bottom-actions {{
            display: flex;
            justify-content: flex-end;
            margin-top: 1rem;
        }}
        .results-table th,
        .results-table td {{
            vertical-align: middle;
        }}
        .results-table input,
        .results-table select {{
            margin-bottom: 0;
        }}
        .remove-row-button {{
            margin-bottom: 0;
            padding-left: 0.6rem;
            padding-right: 0.6rem;
        }}
        .dialog-actions {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 0.75rem;
        }}
        .dialog-actions-right {{
            display: flex;
            gap: 0.75rem;
        }}
    </style>
</html>"""


def handler(event, context):
    method = _http_method(event)
    path = _request_path(event).rstrip("/") or "/"
    is_results_endpoint = path.endswith("/results")
    logger.info("Incoming request: method='%s' path='%s' is_results_endpoint=%s", method, path, is_results_endpoint)

    if method == "POST" and is_results_endpoint:
        try:
            submitted_results = _parse_json_body(event)
            save_summary = _upsert_results(TABLE_NAME, submitted_results)
            logger.info("POST /results succeeded: %s", save_summary)
            return _json_response(200, {"ok": True, **save_summary})
        except ValueError as error:
            logger.error("POST /results validation error: %s", str(error))
            return _json_response(400, {"ok": False, "error": str(error)})
        except json.JSONDecodeError as error:
            logger.error("POST /results invalid JSON payload: %s", str(error))
            return _json_response(400, {"ok": False, "error": "Invalid JSON request body."})
        except (BotoCoreError, ClientError):
            logger.exception("POST /results DynamoDB error.")
            return _json_response(500, {"ok": False, "error": "Failed to save results."})
        except Exception:
            logger.exception("POST /results unexpected error.")
            return _json_response(500, {"ok": False, "error": "Failed to save results."})

    try:
        players = _load_players(TABLE_NAME)
        logger.info("Loaded %d players for HTML response.", len(players))
    except (BotoCoreError, ClientError):
        logger.exception("Failed to load players for HTML response.")
        return _html_response(500, "<html><body><h1>Failed to load tournament data.</h1></body></html>")
    except Exception:
        logger.exception("Unexpected error while building HTML response.")
        return _html_response(500, "<html><body><h1>Failed to load tournament data.</h1></body></html>")

    return _html_response(200, _render_html(players))
