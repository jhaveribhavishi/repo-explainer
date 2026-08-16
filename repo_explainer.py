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


def is_probably_text(path: Path) -> bool:
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
