# jobscout

Daily job scraper that polls company career pages, scores new postings against your CV using an LLM, and writes HTML + Markdown reports.

## Requirements

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager

## Quick start

```bash
uv sync --group dev
cp config/profile.md.example config/profile.md
cp config/preferences.yaml.example config/preferences.yaml
cp config/companies.yaml.example config/companies.yaml
cp config/llm.yaml.example config/llm.yaml
uv run jobscout run
uv run pytest
uv run ruff check .
uv run ruff format .
```

On Windows PowerShell, use `Copy-Item` instead of `cp`.

## Configuration

Templates in `config/` are safe to publish. The filled-in copies are gitignored:

| Template | Your copy (gitignored) | What to put in it |
| --- | --- | --- |
| `profile.md.example` | `profile.md` | Your CV |
| `preferences.yaml.example` | `preferences.yaml` | Roles, locations, and prefilter rules |
| `companies.yaml.example` | `companies.yaml` | Companies you want to poll |
| `llm.yaml.example` | `llm.yaml` | Your own LLM provider and model |
| `.env.example` | `.env` | `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`, if you use those providers |

`settings.yaml` holds fetch and path settings and is shared. `companies.yaml` is your company list. The example has one entry per source, with `enabled: false` so a copied file does not fetch until you turn a company on.

`schedule` runs one check per day while the viewer stays open:

```yaml
schedule:
  enabled: true
  at: "23:00"
```

`at` is local time. Start `uv run jobscout serve` once and leave the report tab open. At 23:00 that same process fetches and scores, and the page reloads when the check finishes. If the machine was asleep at 23:00, the next time the viewer is awake it runs that missed check once. `jobscout run --no-serve` is still a one-shot and does not schedule. `--schedule` / `--no-schedule` on `run` and `serve` override the setting for that process.

`llm.yaml` is the only place the provider is chosen. The `cursor` provider calls the Cursor Agent CLI that is already logged in on that machine. That login is outside this repository, so publishing the project does not share a Cursor subscription. Use your own CLI login, or set `provider` to `openai`, `anthropic`, or `ollama`.

## Commands

Every command accepts `--project` / `-p` to point at a project root. The default is the current directory.

### `run` — fetch, score, and write a report

Fetches career pages, scores new postings, writes `out/latest.html`, and starts the local viewer when you run it in a terminal.

```bash
uv run jobscout run
uv run jobscout run --company Amazon
uv run jobscout run --company Amazon --company Mobileye
uv run jobscout run --dry-run
uv run jobscout run --rescore
uv run jobscout run --max-llm-calls 10
uv run jobscout run --no-serve
uv run jobscout run --serve --port 8765 --open
```

| Flag | What it does |
| --- | --- |
| `--company NAME` | Fetch only this company. Repeat the flag for more than one. The name matches `companies.yaml`, ignoring case. |
| `--dry-run` | Fetch and show what would be scored. Does not call the LLM or save a run. |
| `--seed` | Store current postings as already seen, without scoring them. |
| `--rescore` | Score again even when a posting was scored before. |
| `--max-llm-calls N` | Stop scoring after N model calls. |
| `--no-report` | Skip writing the HTML and Markdown report. |
| `--serve` / `--no-serve` | Start or skip the local viewer. A terminal starts it unless you pass `--no-serve`. |
| `--port N` | Viewer port. Default is `8765`. |
| `--host ADDR` | Address the viewer listens on. Default `0.0.0.0` accepts other PCs on the same LAN. `127.0.0.1` is this PC only. |
| `--open` / `--no-open` | Open the viewer in a browser. |
| `--schedule` / `--no-schedule` | Keep or skip the daily check while this viewer is open. The default is `schedule` in `settings.yaml`. |
| `--verbose`, `-v` | Print fetch progress. |

`uv run jobscout seed` is the same as `run --seed`.

### `jobs` — list scored postings already in the database

```bash
uv run jobscout jobs
uv run jobscout jobs --company Amazon
uv run jobscout jobs --min-score 70 --days 7 --limit 20
uv run jobscout jobs --include-closed
```

| Flag | What it does |
| --- | --- |
| `--company NAME` | Only this company. The name must match the stored company exactly. |
| `--min-score N` | Hide scores below N. Default is `0`. |
| `--days N` | Only jobs first seen in the last N days. |
| `--limit N` | Show at most N rows. |
| `--include-closed` | Include postings that disappeared from the career page. |

### `ignore` — hide a posting, or bring it back

The Ignore button in the report hides a job from later reports. These commands list those jobs and restore them.

```bash
uv run jobscout ignore --list
uv run jobscout ignore --undo 372185270850
uv run jobscout ignore --clear
uv run jobscout ignore https://example.com/jobs/123
```

| Flag | What it does |
| --- | --- |
| `--list` | Table of ignored jobs: when, company, title, and a short key. |
| `--undo KEY` | Put that posting back. `KEY` can be the full hash, the short key from `--list`, or the job URL. |
| `--clear` | Restore every ignored posting. |

Reload the viewer after `--undo` or `--clear`. The score is still stored, so the job shows up again on the next page load.

### `applied` — remember that you already applied

The Applied button in the report turns that card green and keeps it in the same list. Later runs stay green, and the posting is not scored again. A green card has an **Unapply** button if the click was a mistake. That clears the green mark.

```bash
uv run jobscout applied --list
uv run jobscout applied --undo 372185270850
uv run jobscout applied --clear
uv run jobscout applied https://example.com/jobs/123
```

| Flag | What it does |
| --- | --- |
| `--list` | Table of applied jobs: when, company, title, and a short key. |
| `--undo KEY` | Clear the applied mark, same as Unapply. `KEY` can be the full hash, the short key from `--list`, or the job URL. |
| `--clear` | Clear every applied mark. |

Marking a job applied removes an ignore mark, and ignoring it removes the applied mark.

### Other

```bash
uv run jobscout status
uv run jobscout report
uv run jobscout serve --port 8765 --open
uv run jobscout detect-cmd https://careers.example.com --add "Example"
```

| Command | What it does |
| --- | --- |
| `status` | Counts of open, closed, ignored, applied, and pending jobs, plus the last run. |
| `report` | Rebuild `out/latest.html` and `out/latest.md` from the database. |
| `serve` | Open the local viewer without fetching. `--port`, `--host`, `--open`, and `--schedule` work the same as on `run`. With the schedule enabled, leave this process and the browser tab open. Another PC on the same LAN can open the address printed as `On this network`. |
| `detect-cmd URL` | Detect the careers-page source from a URL. `--add "Name"` appends a company block to `companies.yaml`. |
