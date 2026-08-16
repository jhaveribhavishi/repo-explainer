# Repo Explainer 🤖

An AI agent that reads any public GitHub repo and explains it in plain
English — what it does, how it's architected, what stack it uses, and how
to run it. Point it at a repo URL: the agent clones the repo, works through
the codebase under a fixed context budget, and hands back a concise Markdown
report. Runtime scales with repo size — a few seconds for a small repo, up
to 30-45 seconds for a large one.

Built as a self-contained demonstration of real agentic orchestration, not
just a thin wrapper around a chat prompt: shallow cloning, file-tree
filtering, a heuristic for picking the highest-signal files, and a hard
character budget to keep every request within a sane context size, all
coordinated around a single well-crafted call to Claude.

## Two modes: fixed pipeline vs. a real agentic loop

There are two ways this tool explores a repo, and they're architecturally
different -- worth understanding, not just a flag name.

**Default mode** is a fixed pipeline: Python decides which files matter
(`collect_file_contents`), Python builds the prompt, and Claude is called
exactly once to reason over the context it's handed. Fast, cheap, and
deterministic -- but the model itself isn't making exploration decisions.

**`--agent` mode** is a real tool-use loop: Claude gets two tools,
`list_directory` and `read_file`, and decides for itself what to look at --
observing each result before choosing its next move, iterating until it has
enough context to write the report. Slower and more expensive per run (each
exploration step is its own API call, capped at `--max-iterations`, default
12), but it's genuinely agentic: the model is doing the exploring, not
Python.

```bash
# Fixed pipeline (fast, default)
python repo_explainer.py pallets/flask

# Real agentic loop -- watch it decide what to explore
python repo_explainer.py pallets/flask --agent
```

In `--agent` mode, each tool call the model makes is printed live to stderr
(`-> read_file('src/flask/app.py')`, etc.) so you can watch its exploration
path in real time.

## What it does

**Default mode:**
1. Clones the target repo (shallow, read-only).
2. Walks the file tree, skipping build artifacts, dependencies, and binaries.
3. Picks a representative, budget-limited set of files (README, config files,
   entry points, etc.) and reads their contents.
4. Sends the repo structure and file contents to Claude with an instruction
   to explain the project like a senior engineer onboarding a teammate.
5. Prints (or saves) a Markdown report with: what the project does, its
   tech stack, its architecture, notable design choices, and how to run it.

**`--agent` mode:** same clone step, then Claude itself calls
`list_directory`/`read_file` in a loop -- typically listing the root,
reading the README and manifest files, then reading whichever source files
it decides matter most -- before writing the same report format.

## Setup

```bash
git clone <this-repo>
cd repo-explainer
pip install -r requirements.txt
cp .env.example .env   # then add your ANTHROPIC_API_KEY
```

Get an API key at [console.anthropic.com](https://console.anthropic.com/settings/keys).

## Usage

```bash
# Print a report to the terminal
python repo_explainer.py https://github.com/pallets/flask

# Save the report to a file
python repo_explainer.py octocat/Hello-World --output report.md

# Preview the exact prompt without calling the API (no key required)
python repo_explainer.py pallets/flask --dry-run

# Get a longer, more detailed report (slower, bigger context budget)
python repo_explainer.py pallets/flask --detailed

# Real agentic loop -- Claude decides what to explore (see above)
python repo_explainer.py pallets/flask --agent
python repo_explainer.py pallets/flask --agent --max-iterations 20
```

By default the report is quick (~200 words, a smaller slice of the repo sent
to the model). Pass `--detailed` for a longer, more detailed analysis at the
cost of a slower response. `--agent` ignores both `--detailed` and
`--dry-run` -- it's a different execution path entirely.

`owner/repo` shorthand works too, not just full URLs.

## Example output (abridged)

```markdown
## What this project does
Flask is a lightweight WSGI web application framework for Python...

## Tech stack
- Python
- Werkzeug (WSGI toolkit)
- Jinja2 (templating)
- Click (CLI)

## Architecture / how it's organized
The core app lives in `src/flask/app.py`, defining the `Flask` class...
```

## Testing

The deterministic logic (URL parsing, secret-file filtering, file-tree
building, the file-selection heuristic) has unit test coverage in `tests/`,
including the `--agent` mode's tools -- in particular the path-traversal
safety check (`_resolve_within_root`), since a model-supplied file path is
the highest-risk input surface in the whole project. The API calls
themselves aren't unit tested since they require network access and a key
-- use `--dry-run` (default mode) to sanity-check the prompt it would send
instead.

```bash
pip install -r requirements-dev.txt
pytest tests/
```

## Why I built this

My background is in network and cloud infrastructure engineering — a field
built around making sense of complex systems quickly: telemetry, topology,
signal-to-noise, root cause. This project applies the same instinct to
codebases instead of networks. I wanted something that actually *does*
something with an LLM beyond chat — orchestrating file I/O, managing a
token budget, and producing a structured, useful artifact from unstructured
input (an arbitrary codebase) — and that I'd genuinely use myself to get
oriented in unfamiliar repos before diving in.

## Limitations / next steps

- Only handles public repos (no auth for private repos yet).
- Default mode's file selection is heuristic (prioritizes README/config/entry
  points) — very large or unconventionally structured repos may get an
  incomplete picture. `--agent` mode addresses this by letting the model
  explore adaptively instead, at the cost of speed and API spend.
- `--agent` mode's iteration budget (`--max-iterations`) is a blunt
  instrument -- it caps cost/time but doesn't let the model signal "I need
  more room" vs. "I'm confident with what I've seen."
- Could be extended with: a simple web UI, support for private repos via a
  GitHub token, or a "compare two repos" mode.

## Tech stack

Python 3.10+, [Anthropic API](https://docs.anthropic.com/) (Claude), `git`.
