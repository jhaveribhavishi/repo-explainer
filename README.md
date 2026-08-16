# Repo Explainer 🤖

An AI agent that reads any public GitHub repo and explains it in plain
English — what it does, how it's architected, what stack it uses, and how
to run it. Point it at a repo URL and get back a concise Markdown report in
seconds.

Built as a small, self-contained demo of an "agentic" workflow: the script
does real work (cloning, scanning, and reasoning about an unfamiliar
codebase) rather than just wrapping a chat prompt.

## What it does

1. Clones the target repo (shallow, read-only).
2. Walks the file tree, skipping build artifacts, dependencies, and binaries.
3. Picks a representative, budget-limited set of files (README, config files,
   entry points, etc.) and reads their contents.
4. Sends the repo structure and file contents to Claude with an instruction
   to explain the project like a senior engineer onboarding a teammate.
5. Prints (or saves) a Markdown report with: what the project does, its
   tech stack, its architecture, notable design choices, and how to run it.

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
```

By default the report is quick (~200 words, a smaller slice of the repo sent
to the model). Pass `--detailed` for a longer, more detailed analysis at the
cost of a slower response.

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

## Why I built this

I wanted a small project that actually *does* something with an LLM beyond
chat — orchestrating file I/O, managing a token budget, and producing a
structured, useful artifact from unstructured input (an arbitrary codebase).
It's also genuinely useful: I use it to quickly get oriented in unfamiliar
repos before diving in.

## Limitations / next steps

- Only handles public repos (no auth for private repos yet).
- File selection is heuristic (prioritizes README/config/entry points) —
  very large or unconventionally structured repos may get an incomplete
  picture.
- Could be extended with: a simple web UI, support for private repos via a
  GitHub token, or a "compare two repos" mode.

## Tech stack

Python 3.10+, [Anthropic API](https://docs.anthropic.com/) (Claude), `git`.
