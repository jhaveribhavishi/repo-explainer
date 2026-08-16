#!/usr/bin/env python3
"""
Repo Explainer — an AI agent that reads a public GitHub repo and produces
a plain-English explanation: what it does, how it's built, and how to run it.

Usage:
    python repo_explainer.py https://github.com/owner/repo
    python repo_explainer.py https://github.com/owner/repo --output report.md
    python repo_explainer.py https://github.com/owner/repo --dry-run   # no API key needed

Requires an ANTHROPIC_API_KEY environment variable (or .env file) unless --dry-run is used.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# ---- Configuration ---------------------------------------------------

MAX_FILES_TO_INCLUDE = 25          # cap how many file contents we send to the model
MAX_CHARS_PER_FILE = 4000          # truncate long files
MAX_TOTAL_PROMPT_CHARS = 60000     # rough overall budget for repo content in the prompt
MODEL = "claude-sonnet-4-5"

# Files/directories we never want to look inside (build artifacts, deps, binaries, VCS)
SKIP_DIR_NAMES = {
    ".git", "node_modules", "vendor", "dist", "build", "target",
    "__pycache__", ".venv", "venv", ".next", ".cache", "coverage",
    ".idea", ".vscode", "bin", "obj",
}
SKIP_FILE_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".pdf", ".zip",
    ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot", ".mp4", ".mov",
    ".exe", ".dll", ".so", ".dylib", ".class", ".jar", ".lock",
    ".min.js", ".min.css",
}

# Filenames that commonly hold secrets. Even though we only read files from
# a fresh clone (not the user's own machine), a repo can accidentally have
# these committed -- we never want to read them, let alone ship their
# contents to a third-party API as part of the prompt.
SKIP_SECRET_FILENAMES = {
    ".env", ".env.local", ".env.development", ".env.production",
    "credentials.json", "credentials.yml", "credentials.yaml",
    "secrets.json", "secrets.yml", "secrets.yaml",
    "id_rsa", "id_rsa.pub", "id_ed25519", "id_ed25519.pub",
    ".npmrc", ".pypirc", ".netrc",
}
SKIP_SECRET_SUFFIXES = (".pem", ".key", ".pfx", ".p12", ".keystore")

# Files we always prioritize including in full if present (highest value signal)
PRIORITY_FILES = [
    "README.md", "README.rst", "README.txt", "README",
    "package.json", "pyproject.toml", "requirements.txt", "setup.py",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle",
    "Dockerfile", "docker-compose.yml", "Makefile",
    "main.py", "app.py", "index.js", "index.ts", "main.go", "src/main.rs",
]


def run(cmd, **kwargs):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def normalize_github_url(url: str) -> str:
    """Accept a few common forms and normalize to a cloneable https URL."""
    url = url.strip()
    match = re.match(r"^(?:https?://)?(?:www\.)?github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", url)
    if match:
        owner, repo = match.groups()
        return f"https://github.com/{owner}/{repo}.git"
    match = re.match(r"^([\w.-]+)/([\w.-]+)$", url)
    if match:
        owner, repo = match.groups()
        return f"https://github.com/{owner}/{repo}.git"
    raise ValueError(f"Doesn't look like a GitHub repo reference: {url!r}")


def clone_repo(url: str, dest: Path) -> None:
    print(f"Cloning {url} ...", file=sys.stderr)
    # --depth 1: only the latest commit, not full history.
    # --single-branch: skip fetching refs for every other branch.
    # (Tried --filter=blob:none too -- a "partial clone" that defers
    # downloading file contents. Benchmarked it on google/perfetto: 12s vs
    # 4.6s for a plain shallow clone, i.e. SLOWER. The filter only pays off
    # if you also skip checkout via sparse-checkout, since a normal checkout
    # has to fetch every blob in the tree regardless. Not worth the added
    # complexity here, so left out.)
    run(["git", "clone", "--depth", "1", "--single-branch", "--quiet", url, str(dest)])


def build_file_tree(root: Path, max_entries: int = 400) -> str:
    """Produce an indented text tree of the repo, skipping noisy directories."""
    lines = []
    count = 0

    def walk(dir_path: Path, prefix: str = ""):
        nonlocal count
        try:
            entries = sorted(
                dir_path.iterdir(),
                key=lambda p: (p.is_file(), p.name.lower()),
            )
        except PermissionError:
            return
        for entry in entries:
            if count >= max_entries:
                return
            if entry.name.startswith(".") and entry.name not in {".github"}:
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                lines.append(f"{prefix}{entry.name}/")
                count += 1
                walk(entry, prefix + "  ")
            else:
                lines.append(f"{prefix}{entry.name}")
                count += 1

    walk(root)
    if count >= max_entries:
        lines.append("... (truncated, repo has more files)")
    return "\n".join(lines)


def is_probably_secret(path: Path) -> bool:
    """True if this file commonly holds credentials and must never be read,
    even if it happens to be committed in the target repo."""
    if path.name in SKIP_SECRET_FILENAMES:
        return True
    if path.name.endswith(SKIP_SECRET_SUFFIXES):
        return True
    return False


def is_probably_text(path: Path) -> bool:
    if is_probably_secret(path):
        return False
    if any(path.name.endswith(suf) for suf in SKIP_FILE_SUFFIXES):
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(1024)
        if b"\x00" in chunk:
            return False
        return True
    except OSError:
        return False


def collect_file_contents(
    root: Path,
    max_files: int = MAX_FILES_TO_INCLUDE,
    max_total_chars: int = MAX_TOTAL_PROMPT_CHARS,
) -> list[tuple[str, str]]:
    """Pick a representative, budget-limited set of files and read their contents."""
    all_files = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES and not d.startswith(".")]
        for fname in filenames:
            fpath = Path(dirpath) / fname
            rel = fpath.relative_to(root).as_posix()
            all_files.append((rel, fpath))

    def priority_key(item):
        rel, _ = item
        for i, pf in enumerate(PRIORITY_FILES):
            if rel == pf or rel.endswith("/" + pf):
                return (0, i)
        # prefer shallow files (likely entry points / config)
        return (1, rel.count("/"))

    all_files.sort(key=priority_key)

    selected = []
    total_chars = 0
    for rel, fpath in all_files:
        if len(selected) >= max_files:
            break
        if not is_probably_text(fpath):
            continue
        try:
            text = fpath.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        if not text.strip():
            continue
        truncated = text[:MAX_CHARS_PER_FILE]
        if total_chars + len(truncated) > max_total_chars:
            break
        selected.append((rel, truncated))
        total_chars += len(truncated)

    return selected


def build_prompt(repo_url: str, tree: str, files: list[tuple[str, str]], word_limit: int = 500) -> str:
    files_block = "\n\n".join(
        f"--- FILE: {rel} ---\n{content}" for rel, content in files
    )
    return f"""You are an expert software engineer explaining a codebase to another engineer
who has never seen it before. Be concrete and specific — cite actual file and
function/class names you saw. Do not pad with generic filler.

Repo: {repo_url}

FILE TREE (partial):
{tree}

SELECTED FILE CONTENTS:
{files_block}

Write a report in Markdown with these sections:

## What this project does
2-4 sentences, plain English, no jargon dump.

## Tech stack
Bullet list of languages, frameworks, and key libraries actually used (infer from
config files and imports you saw — don't guess wildly).

## Architecture / how it's organized
Explain the major components/modules and how they relate. Reference real
file or directory names.

## Notable design choices or patterns
1-3 things a reviewer would find interesting or worth learning from.

## How to run it
Best-effort setup/run instructions based on what you saw (README, package.json
scripts, Dockerfile, etc). If unclear, say so plainly rather than inventing steps.

Keep the whole report under {word_limit} words.
"""


def call_claude(prompt: str, model: str, print_live: bool = False, max_tokens: int = 2000) -> str:
    try:
        import anthropic
    except ImportError:
        print(
            "The 'anthropic' package isn't installed. Run: pip install -r requirements.txt",
            file=sys.stderr,
        )
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print(
            "Set ANTHROPIC_API_KEY (env var or .env file) to run this for real, "
            "or use --dry-run to preview the prompt without calling the API.",
            file=sys.stderr,
        )
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)

    # Stream the response instead of blocking until it's fully generated.
    # Two wins: (1) perceived latency drops to near-zero -- text starts
    # appearing the moment the model starts writing, instead of after the
    # full ~500-word report is done; (2) if the terminal is the output
    # target, we print tokens live so a demo/recording never sits on a
    # blank "thinking" screen.
    chunks = []
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        for text in stream.text_stream:
            chunks.append(text)
            if print_live:
                print(text, end="", flush=True)
        if print_live:
            print()  # trailing newline after streaming finishes
    return "".join(chunks)


# =========================================================================
# Agentic mode (--agent): a real tool-use loop.
#
# Everything above this point is a FIXED pipeline: Python decides which
# files matter (collect_file_contents), Python builds the prompt, Claude
# only reasons over what it's handed. That's fast and cheap, but it isn't
# an "agent" in the strict sense -- there's no autonomous, iterative
# decision-making by the model.
#
# This section is the real thing: Claude gets two tools (list_directory,
# read_file) and decides for itself what to explore, in a loop, observing
# each result before deciding its next move -- until it has enough context
# to write the report. Slower and more expensive per run (each exploration
# step is its own API round trip), but it's genuinely agentic.
# =========================================================================

AGENT_TOOLS = [
    {
        "name": "list_directory",
        "description": (
            "List files and subdirectories at a path inside the repo, relative "
            "to the repo root. Use '' or '.' for the root. Noisy directories "
            "(dependencies, build output, VCS internals) are already filtered out."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "read_file",
        "description": (
            "Read the contents of a single text file at a path relative to the "
            "repo root. Binary files and files that commonly hold secrets "
            "(.env, credentials, private keys) will be refused. Long files are "
            "truncated."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative path from repo root"},
            },
            "required": ["path"],
        },
    },
]


def _resolve_within_root(root: Path, rel_path: str) -> Path | None:
    """Resolve a model-supplied relative path safely inside root.
    Returns None if the path tries to escape the repo (e.g. '../../etc/passwd')."""
    candidate = (root / rel_path.strip().lstrip("/")).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def tool_list_directory(root: Path, rel_path: str) -> str:
    target = _resolve_within_root(root, rel_path or ".")
    if target is None:
        return "Error: path escapes the repository root -- not allowed."
    if not target.exists():
        return f"Error: no such path: {rel_path!r}"
    if not target.is_dir():
        return f"Error: {rel_path!r} is a file, not a directory. Use read_file instead."

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
            if entry.name.startswith(".") and entry.name != ".github":
                continue
            if entry.is_dir():
                if entry.name in SKIP_DIR_NAMES:
                    continue
                entries.append(f"{entry.name}/")
            else:
                entries.append(entry.name)
    except PermissionError:
        return f"Error: permission denied reading {rel_path!r}"

    if not entries:
        return "(empty directory, or everything here was filtered out)"
    return "\n".join(entries)


def tool_read_file(root: Path, rel_path: str) -> str:
    target = _resolve_within_root(root, rel_path)
    if target is None:
        return "Error: path escapes the repository root -- not allowed."
    if not target.exists():
        return f"Error: no such file: {rel_path!r}"
    if target.is_dir():
        return f"Error: {rel_path!r} is a directory. Use list_directory instead."
    if is_probably_secret(target):
        return (
            f"Refused: {rel_path!r} matches a pattern commonly used for secrets "
            f"(credentials, keys, .env files). This tool will not read it."
        )
    if not is_probably_text(target):
        return f"Refused: {rel_path!r} looks like a binary file, not source/text."

    try:
        text = target.read_text(encoding="utf-8", errors="ignore")
    except OSError as e:
        return f"Error reading {rel_path!r}: {e}"

    if len(text) > MAX_CHARS_PER_FILE:
        text = text[:MAX_CHARS_PER_FILE] + "\n... (truncated)"
    return text


AGENT_SYSTEM_PROMPT = """You are an expert software engineer exploring an unfamiliar codebase to \
write a technical report for another engineer who has never seen it. You have two tools:

- list_directory(path): list files/subdirectories at a path relative to the repo root
- read_file(path): read a text file's contents, relative to the repo root

Explore efficiently. A good approach: list the root, read the README and key config/manifest \
files (package.json, pyproject.toml, Cargo.toml, go.mod, etc.), then read 5-10 of the most \
important source files based on what you learn. You have a limited tool-call budget -- don't \
explore exhaustively, prioritize the highest-signal files.

Once you have enough context, STOP calling tools and write your final report as plain text \
(no further tool calls) in this Markdown format:

## What this project does
2-4 sentences, plain English.

## Tech stack
Bullet list of languages, frameworks, and key libraries actually observed.

## Architecture / how it's organized
Explain the major components/modules and how they relate. Reference real file/directory names \
you actually read.

## Notable design choices or patterns
1-3 things a reviewer would find interesting, based on files you actually read -- not generic \
guesses.

## How to run it
Best-effort setup/run instructions based on what you saw.

Be concrete and specific -- cite actual file and function/class names you read, not guesses. \
Keep the whole report under 400 words."""


def run_agent(
    repo_url: str,
    root: Path,
    model: str,
    max_iterations: int = 12,
    print_live: bool = False,
) -> str:
    """The real agentic loop: Claude decides what to explore, calls tools,
    observes results, and decides its next move -- until it writes the
    final report or hits the iteration budget."""
    try:
        import anthropic
    except ImportError:
        print("The 'anthropic' package isn't installed. Run: pip install -r requirements.txt", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY (env var or .env file) to run --agent mode.", file=sys.stderr)
        sys.exit(1)

    client = anthropic.Anthropic(api_key=api_key)
    messages = [{"role": "user", "content": f"Repo: {repo_url}\n\nExplore it and write the report."}]

    for iteration in range(1, max_iterations + 1):
        # On the final allowed iteration, force a written answer instead of
        # another tool call, so we always end with a real report rather than
        # silently running out of budget mid-exploration.
        force_answer = iteration == max_iterations
        response = client.messages.create(
            model=model,
            max_tokens=1500,
            system=AGENT_SYSTEM_PROMPT,
            tools=AGENT_TOOLS,
            tool_choice={"type": "auto"} if not force_answer else {"type": "none"},
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            report = "".join(b.text for b in response.content if hasattr(b, "text"))
            if print_live:
                print(report)
            return report

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            path_arg = block.input.get("path", "")
            if block.name == "list_directory":
                if print_live:
                    print(f"  -> list_directory({path_arg!r})", file=sys.stderr)
                result = tool_list_directory(root, path_arg)
            elif block.name == "read_file":
                if print_live:
                    print(f"  -> read_file({path_arg!r})", file=sys.stderr)
                result = tool_read_file(root, path_arg)
            else:
                result = f"Error: unknown tool {block.name!r}"
            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": result})
        messages.append({"role": "user", "content": tool_results})

    # Should be unreachable: the force_answer branch on the last iteration
    # always returns. Kept as a safety net.
    return "Agent ran out of iterations without producing a final report."


def main():
    parser = argparse.ArgumentParser(description="Explain a GitHub repo in plain English using Claude.")
    parser.add_argument("repo", help="GitHub repo URL or 'owner/repo' shorthand")
    parser.add_argument("--output", "-o", default=None, help="Write report to this file (default: print to stdout)")
    parser.add_argument("--model", default=MODEL, help=f"Claude model to use (default: {MODEL})")
    parser.add_argument("--dry-run", action="store_true", help="Build the prompt but skip the API call (no key needed)")
    parser.add_argument(
        "--detailed", action="store_true",
        help="Trade speed for detail: longer report (~500 words), bigger "
             "context budget (more files, more content per file). Slower, but "
             "more detailed. The default is the quick (~200 word) report.",
    )
    parser.add_argument(
        "--agent", action="store_true",
        help="Use a real agentic loop instead of the fixed pipeline: Claude "
             "itself decides which files to explore, via list_directory/"
             "read_file tool calls, iterating until it has enough context to "
             "write the report. Slower and more expensive per run (each "
             "exploration step is its own API call), but genuinely agentic "
             "rather than a single call over pre-selected context. Ignores "
             "--detailed and --dry-run.",
    )
    parser.add_argument(
        "--max-iterations", type=int, default=12,
        help="With --agent, the max number of tool-call rounds before the "
             "agent is forced to write its final report (default: 12).",
    )
    args = parser.parse_args()

    if args.detailed:
        word_limit, max_tokens = 500, 2000
        max_files, max_total_chars = MAX_FILES_TO_INCLUDE, MAX_TOTAL_PROMPT_CHARS
    else:
        word_limit, max_tokens = 200, 700
        max_files, max_total_chars = 12, 20000

    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    repo_url = normalize_github_url(args.repo)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        try:
            clone_repo(repo_url, tmp_path)
        except subprocess.CalledProcessError as e:
            print(f"Failed to clone {repo_url}:\n{e.stderr}", file=sys.stderr)
            sys.exit(1)

        if args.agent:
            print(f"Agent exploring the repo (up to {args.max_iterations} steps)...", file=sys.stderr)
            print_live = not args.output
            report = run_agent(repo_url, tmp_path, args.model, max_iterations=args.max_iterations, print_live=print_live)
            if args.output:
                Path(args.output).write_text(report, encoding="utf-8")
                print(f"Report written to {args.output}", file=sys.stderr)
            return

        print("Scanning repo structure...", file=sys.stderr)
        tree = build_file_tree(tmp_path)

        print("Selecting representative files...", file=sys.stderr)
        files = collect_file_contents(tmp_path, max_files=max_files, max_total_chars=max_total_chars)
        if not files:
            print("Warning: no readable text files found to analyze.", file=sys.stderr)

        prompt = build_prompt(repo_url, tree, files, word_limit=word_limit)

        if args.dry_run:
            print(f"--- DRY RUN: prompt is {len(prompt)} chars, {len(files)} files included ---\n", file=sys.stderr)
            print(prompt)
            return

        print(f"Asking {args.model} to explain the repo...", file=sys.stderr)
        # Stream live to the terminal only when we're not also about to dump
        # the same text to a file (avoids printing the report twice).
        print_live = not args.output
        report = call_claude(prompt, args.model, print_live=print_live, max_tokens=max_tokens)

    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"Report written to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
