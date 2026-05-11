import pandas as pd
import requests
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from jinja2 import Template
import zipfile
import time
import unicodedata
import html as html_escape

SEASON_NAME = "20252026"
PLAYOFF_BRACKET_URL = "https://api-web.nhle.com/v1/playoff-bracket/2026"
CSV_URL = "https://docs.google.com/spreadsheets/d/e/2PACX-1vRwp6OncSv2CpUxjFNsJNZ7gG5BBKUIVNYHoFwR7TTJstb-mpGNQYmYwyizlRRalA/pub?output=csv"

OUT_DIR = Path(".")
OUT_DIR.mkdir(parents=True, exist_ok=True)

HTML_FILE = OUT_DIR / "index.html"

TEAM_MAP = {"TB": "TBL", "TBL": "TBL"}

POSITION_ORDER = {
    "F": 1, "C": 1,
    "L": 2, "LW": 2,
    "R": 3, "RW": 3,
    "D": 4,
    "G": 5,
}

DRAFT_ORDER_MAP = {
    "James": 1,
    "Alyssa": 2,
    "Xavi": 3,
    "Coltrane": 4,
    "Luka": 5,
    "Dave": 6,
    "Tom": 7,
    "Anuja": 8,
    "Kia": 9,
    "Dyuman": 10,
}

def normalize_name(name):
    if pd.isna(name):
        return ""
    name = str(name).strip()
    return unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii").lower()

def clean_team(team):
    if pd.isna(team):
        return ""
    team = str(team).strip().upper()
    return TEAM_MAP.get(team, team)

def clean_position(pos):
    if pd.isna(pos):
        return ""
    pos = str(pos).strip().upper()

    if pos == "LW":
        return "L"

    if pos == "RW":
        return "R"

    return pos

def esc(x):
    return html_escape.escape(str(x))

def get_json(url, sleep=0.6, retries=5, label="request"):
    for attempt in range(1, retries + 1):
        try:
            time.sleep(sleep)

            r = requests.get(url, timeout=45)

            if r.status_code == 429:
                wait = 5 + attempt * 5
                print(f"Rate limited on {label}. Waiting {wait}s...")
                time.sleep(wait)
                continue

            if r.status_code in [500, 502, 503, 504]:
                wait = 4 + attempt * 4
                print(f"NHL API temporary error on {label}. Waiting {wait}s...")
                time.sleep(wait)
                continue

            r.raise_for_status()
            return r.json()

        except Exception as e:
            if attempt == retries:
                raise e

            wait = 3 + attempt * 3
            print(f"Request failed on {label}. Waiting {wait}s...")
            time.sleep(wait)

# -----------------------------
# LOAD GOOGLE SHEET CSV
# -----------------------------

print("Loading Google Sheet CSV...")

pool = pd.read_csv(CSV_URL)

needed_cols = ["Pick", "Player", "Name", "Position", "Team"]

missing = [c for c in needed_cols if c not in pool.columns]

if missing:
    raise ValueError(f"Missing required columns in CSV: {missing}")

pool = pool[needed_cols].copy()

pool["Player"] = pool["Player"].astype(str).str.strip()
pool["Name"] = pool["Name"].astype(str).str.strip()
pool["Team"] = pool["Team"].apply(clean_team)
pool["Position"] = pool["Position"].apply(clean_position)
pool["Player_clean"] = pool["Player"].apply(normalize_name)

# -----------------------------
# ADD DRAFT ORDER
# -----------------------------

pool["Draft Order"] = pool["Name"].map(DRAFT_ORDER_MAP)

cols = pool.columns.tolist()

name_idx = cols.index("Name")

cols.insert(
    name_idx + 1,
    cols.pop(cols.index("Draft Order"))
)

pool = pool[cols]

print(f"Loaded {len(pool)} picks.")

# -----------------------------
# GET NHL PLAYER METADATA
# -----------------------------

def get_roster_for_team(team_abbr):
    url = f"https://api-web.nhle.com/v1/roster/{team_abbr}/{SEASON_NAME}"

    data = get_json(
        url,
        sleep=0.5,
        retries=5,
        label=f"roster {team_abbr}"
    )

    rows = []

    for group_name in ["forwards", "defensemen", "goalies"]:
        for p in data.get(group_name, []):
            first = p.get("firstName", {}).get("default", "")
            last = p.get("lastName", {}).get("default", "")

            name = f"{first} {last}".strip()

            rows.append({
                "Player_api": name,
                "Player_clean": normalize_name(name),
                "Team": team_abbr,
                "id": p.get("id"),
                "api_position": p.get("positionCode"),
            })

    return pd.DataFrame(rows)

team_names = sorted(pool["Team"].dropna().unique())

metadata_frames = []

print("Fetching NHL rosters...")

for i, team in enumerate(team_names, start=1):
    try:
        metadata_frames.append(get_roster_for_team(team))
        print(f"[{i}/{len(team_names)}] Fetched roster: {team}")

    except Exception as e:
        print(f"[{i}/{len(team_names)}] Could not fetch roster for {team}: {e}")

player_metadata = pd.concat(metadata_frames, ignore_index=True)

looper = pool.merge(
    player_metadata[["Player_clean", "Team", "id", "api_position", "Player_api"]],
    on=["Player_clean", "Team"],
    how="left"
)

missing_ids = looper[looper["id"].isna()][["Player", "Team", "Position", "Name"]]

if len(missing_ids):
    print("WARNING: These players did not match NHL roster metadata:")
    print(missing_ids)

else:
    print("All players matched to NHL IDs.")

# -----------------------------
# GET PLAYOFF STATS
# -----------------------------

def get_player_stats(row, i=None, total=None):
    player_name = row["Player"]
    player_team = row["Team"]
    player_position = row["Position"]
    player_id = row["id"]

    prefix = f"[{i}/{total}] " if i is not None and total is not None else ""

    if pd.isna(player_id):
        print(f"{prefix}Missing NHL ID: {player_name}")

        return {
            "Player": player_name,
            "Team": player_team,
            "Goals": 0,
            "Assists": 0,
            "Wins": 0,
            "Shutouts": 0,
            "Points": 0,
        }

    url = f"https://api-web.nhle.com/v1/player/{int(player_id)}/game-log/{SEASON_NAME}/3"

    try:
        print(f"{prefix}Fetching stats: {player_name}")

        data = get_json(
            url,
            sleep=1.1,
            retries=6,
            label=f"stats {player_name}"
        )

        game_log = data.get("gameLog", [])

        if not game_log:
            goals = assists = wins = shutouts = 0

        else:
            df = pd.DataFrame(game_log)

            goals = df.get("goals", pd.Series(dtype=float)).fillna(0).sum()
            assists = df.get("assists", pd.Series(dtype=float)).fillna(0).sum()

            if player_position == "G":
                wins = (df.get("decision", pd.Series(dtype=str)) == "W").sum()

                shutouts = (
                    df.get("shutouts", pd.Series(dtype=float))
                    .fillna(0)
                    .sum()
                )

            else:
                wins = 0
                shutouts = 0

        points = goals + assists + wins + shutouts * 3

        return {
            "Player": player_name,
            "Team": player_team,
            "Goals": int(goals),
            "Assists": int(assists),
            "Wins": int(wins),
            "Shutouts": int(shutouts),
            "Points": int(points),
        }

    except Exception as e:
        print(f"{prefix}Stats error for {player_name}: {e}")

        return {
            "Player": player_name,
            "Team": player_team,
            "Goals": 0,
            "Assists": 0,
            "Wins": 0,
            "Shutouts": 0,
            "Points": 0,
        }

print("Fetching player playoff stats...")

total_players = len(looper)

stats_rows = []

for i, (_, row) in enumerate(looper.iterrows(), start=1):
    stats_rows.append(
        get_player_stats(
            row,
            i=i,
            total=total_players
        )
    )

stats = pd.DataFrame(stats_rows)

print("Finished fetching player stats.")

# -----------------------------
# GET ELIMINATED TEAMS
# -----------------------------

def get_eliminated_teams():
    try:
        all_teams_data = get_json(
            "https://api.nhle.com/stats/rest/en/team",
            sleep=0.5,
            retries=5,
            label="all teams"
        )

        bracket = get_json(
            PLAYOFF_BRACKET_URL,
            sleep=0.5,
            retries=5,
            label="playoff bracket"
        )

        losing_ids = [
            s.get("losingTeamId")
            for s in bracket.get("series", [])
            if s.get("losingTeamId") is not None
        ]

        teams_df = pd.DataFrame(all_teams_data.get("data", []))

        if not losing_ids or teams_df.empty:
            return []

        return (
            teams_df[teams_df["id"].isin(losing_ids)]["triCode"]
            .dropna()
            .astype(str)
            .tolist()
        )

    except Exception as e:
        print(f"Could not fetch eliminated teams: {e}")
        return []

eliminated_teams = get_eliminated_teams()

print(f"Eliminated teams: {eliminated_teams}")

# -----------------------------
# BUILD RANKINGS
# -----------------------------

joined = pool.merge(
    stats,
    on=["Player", "Team"],
    how="left"
)

for col in ["Goals", "Assists", "Wins", "Shutouts", "Points"]:
    joined[col] = joined[col].fillna(0).astype(int)

joined["Pos"] = joined["Position"]

joined["Player Status"] = joined["Team"].isin(eliminated_teams)

joined["rank_pos"] = joined["Pos"].map(POSITION_ORDER).fillna(99)

joined = joined.sort_values(
    ["Name", "rank_pos", "Points"],
    ascending=[True, True, False]
)

rankings = (
    joined.groupby("Name", as_index=False)
    .agg(
        **{
            "Draft Order": ("Draft Order", "first"),
            "Points": ("Points", "sum"),
            "Goals": ("Goals", "sum"),
            "Players Left": ("Player Status", lambda x: int((~x).sum())),
        }
    )
    .sort_values(["Points", "Goals"], ascending=[False, False])
    .reset_index(drop=True)
)

rankings.insert(0, "Rank", range(1, len(rankings) + 1))

rankings = rankings[
    ["Rank", "Name", "Draft Order", "Points", "Goals", "Players Left"]
]

# -----------------------------
# HTML HELPERS
# -----------------------------

def make_leaderboard_html(rankings_df):
    df = rankings_df.copy()

    html = ['<table class="leaderboard">']

    html.append("<thead><tr>")

    for col in df.columns:
        html.append(f"<th>{esc(col)}</th>")

    html.append("</tr></thead><tbody>")

    for _, row in df.iterrows():
        rank = row["Rank"]

        cls = ""

        if rank == 1:
            cls = "gold"

        elif rank == 2:
            cls = "silver"

        elif rank == 3:
            cls = "bronze"

        html.append(f'<tr class="{cls}">')

        for col in df.columns:
            html.append(f"<td>{esc(row[col])}</td>")

        html.append("</tr>")

    html.append("</tbody></table>")

    return "\n".join(html)

def make_contestant_table_html(name, df):
    show_cols = [
        "Player",
        "Pos",
        "Team",
        "Goals",
        "Assists",
        "Wins",
        "Shutouts",
        "Points",
        "Player Status"
    ]

    sub = df[df["Name"] == name].copy()

    sub = sub.sort_values(
        ["rank_pos", "Points"],
        ascending=[True, False]
    )

    sub = sub[show_cols].copy()

    total_points = int(sub["Points"].sum())

    remaining = int((~sub["Player Status"]).sum())

    html = []

    html.append('<section class="team-card">')

    html.append(
        f"<h3>{esc(name)} "
        f"<span>(Total Points: {total_points})</span></h3>"
    )

    html.append(
        f'<p class="remaining">Players Left: {remaining}</p>'
    )

    html.append('<table class="team-table">')

    html.append("<thead><tr>")

    for col in show_cols:
        html.append(f"<th>{esc(col)}</th>")

    html.append("</tr></thead><tbody>")

    for _, row in sub.iterrows():
        html.append("<tr>")

        for col in show_cols:
            value = row[col]

            if (
                col in ["Wins", "Shutouts"]
                and row["Pos"] != "G"
                and int(value) == 0
            ):
                value = "-"

            if col == "Player Status":
                if bool(value):
                    html.append('<td class="eliminated">Eliminated</td>')

                else:
                    html.append('<td class="remaining-cell">Remaining</td>')

            elif col == "Points":
                html.append(
                    f'<td class="points">{esc(value)}</td>'
                )

            else:
                html.append(f"<td>{esc(value)}</td>")

        html.append("</tr>")

    # TOTAL ROW

    total_goals = int(sub["Goals"].sum())
    total_assists = int(sub["Assists"].sum())
    total_wins = int(sub["Wins"].sum())
    total_shutouts = int(sub["Shutouts"].sum())
    total_points = int(sub["Points"].sum())

    html.append('<tr class="total-row">')

    html.append("<td><strong>Total</strong></td>")
    html.append("<td></td>")
    html.append("<td></td>")

    html.append(f"<td><strong>{total_goals}</strong></td>")
    html.append(f"<td><strong>{total_assists}</strong></td>")
    html.append(f"<td><strong>{total_wins}</strong></td>")
    html.append(f"<td><strong>{total_shutouts}</strong></td>")

    html.append(
        f'<td class="points"><strong>{total_points}</strong></td>'
    )

    html.append("<td></td>")

    html.append("</tr>")

    html.append("</tbody></table>")

    html.append("</section>")

    return "\n".join(html)

# -----------------------------
# CREATE STATIC HTML SITE
# -----------------------------

last_updated = datetime.now(
    ZoneInfo("America/Toronto")
).strftime("%B %d, %Y at %I:%M %p")

leaderboard_html = make_leaderboard_html(rankings)

contestant_tables_html = "\n".join(
    make_contestant_table_html(name, joined)
    for name in rankings["Name"]
)

template = Template(r'''
<!doctype html>
<html lang="en">

<head>

<meta charset="utf-8">

<title>2026 "TOOTHLESS HOCKEY" NHL Playoff Pool</title>

<meta name="viewport" content="width=device-width, initial-scale=1">

<style>

:root {
  --bg: #fafafa;
  --card: #ffffff;
  --text: #1f2937;
  --muted: #6b7280;
  --border: #e5e7eb;
  --points: #f9e3d6;
  --green: #48dc03;
  --red: #ff5733;
}

body {
  margin: 0;
  font-family: Arial, Helvetica, sans-serif;
  background: var(--bg);
  color: var(--text);
  line-height: 1.4;
}

header {
  padding: 32px 20px 20px;
  max-width: 1100px;
  margin: 0 auto;
}

h1 {
  margin: 0 0 6px;
  font-size: 34px;
  letter-spacing: -0.03em;
}

.updated {
  color: var(--muted);
  margin: 0;
}

main {
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 20px 48px;
}

.tabs {
  display: flex;
  gap: 8px;
  margin: 18px 0;
  flex-wrap: wrap;
}

.tab-button {
  border: 1px solid var(--border);
  background: white;
  padding: 10px 14px;
  border-radius: 999px;
  cursor: pointer;
  font-weight: 700;
}

.tab-button.active {
  background: #111827;
  color: white;
  border-color: #111827;
}

.tab-content {
  display: none;
}

.tab-content.active {
  display: block;
}

table {
  width: 100%;
  border-collapse: collapse;
  background: white;
  margin-bottom: 24px;
  font-size: 14px;
}

th, td {
  padding: 10px 12px;
  border-bottom: 1px solid var(--border);
  text-align: left;
  border-right: 1px dashed #d1d5db;
}

th {
  background: #f3f4f6;
  font-weight: 800;
}

.leaderboard tr.gold td {
  background: gold;
  font-weight: 800;
}

.leaderboard tr.silver td {
  background: #c0c0c0;
  font-weight: 800;
}

.leaderboard tr.bronze td {
  background: #cd7f32;
  font-weight: 800;
}

.team-card {
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: 16px;
  padding: 18px;
  margin: 18px 0;
  box-shadow: 0 2px 10px rgba(0,0,0,0.04);
  overflow-x: auto;
}

.team-card h3 {
  margin: 0;
  font-size: 22px;
}

.team-card h3 span {
  color: var(--muted);
  font-size: 16px;
  font-weight: 600;
}

.remaining {
  margin: 4px 0 14px;
  color: var(--muted);
  font-weight: 700;
}

.points {
  background: var(--points);
  font-weight: 800;
}

.eliminated {
  background: var(--red);
  color: white;
  font-style: italic;
  font-weight: 800;
}

.remaining-cell {
  background: var(--green);
  color: white;
  font-weight: 800;
}

.total-row td {
  background: #f3f4f6;
  font-weight: 800;
}

.note {
  color: var(--muted);
  font-size: 13px;
  margin-top: 24px;
}

</style>

</head>

<body>

<header>
  <h1>2026 "TOOTHLESS HOCKEY" NHL Playoff Pool</h1>
  <p class="updated">Last updated on {{ last_updated }} ET</p>
</header>

<main>

<div class="tabs">
  <button class="tab-button active" data-tab="leaderboard">
    Leader Board
  </button>

  <button class="tab-button" data-tab="teams">
    Playoff Pool Teams
  </button>
</div>

<section id="leaderboard" class="tab-content active">
  {{ leaderboard_html }}
</section>

<section id="teams" class="tab-content">
  {{ contestant_tables_html }}
</section>

<p class="note">
  Scoring: skaters get goals + assists.
  Goalies get goals + assists + wins + three points per shutout.
</p>

</main>

<script>

const buttons = document.querySelectorAll(".tab-button");
const tabs = document.querySelectorAll(".tab-content");

buttons.forEach(button => {
  button.addEventListener("click", () => {
    buttons.forEach(b => b.classList.remove("active"));
    tabs.forEach(t => t.classList.remove("active"));

    button.classList.add("active");

    document
      .getElementById(button.dataset.tab)
      .classList.add("active");
  });
});

</script>

</body>
</html>
''')

html = template.render(
    last_updated=last_updated,
    leaderboard_html=leaderboard_html,
    contestant_tables_html=contestant_tables_html,
)

HTML_FILE.write_text(html, encoding="utf-8")

zip_path = Path("nhl_pool_site.zip")

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    for file in OUT_DIR.rglob("*"):
        z.write(file, file.relative_to(OUT_DIR.parent))

print(f"Created: {HTML_FILE}")
print(f"Created: {zip_path}")
print("Done.")
