"""
Unit tests for repo_explainer.py.

Covers the pure/deterministic logic -- URL normalization, secret-file
detection, text-file sniffing, file-tree building, and the file-selection
heuristic. Deliberately does NOT test call_claude() or clone_repo(), since
those require network access and an API key; that logic is exercised by
--dry-run in manual testing instead.

Run with:
    pip install pytest
    pytest tests/
"""

import sys
from pathlib import Path

import pytest

# Make the top-level script importable as a module.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import repo_explainer as re_mod  # noqa: E402


# ---- normalize_github_url ---------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://github.com/pallets/flask", "https://github.com/pallets/flask.git"),
        ("http://github.com/pallets/flask", "https://github.com/pallets/flask.git"),
        ("https://github.com/pallets/flask/", "https://github.com/pallets/flask.git"),
        ("https://github.com/pallets/flask.git", "https://github.com/pallets/flask.git"),
        ("www.github.com/pallets/flask", "https://github.com/pallets/flask.git"),
        ("pallets/flask", "https://github.com/pallets/flask.git"),
    ],
)
def test_normalize_github_url_accepts_common_forms(raw, expected):
    assert re_mod.normalize_github_url(raw) == expected


def test_normalize_github_url_rejects_garbage():
    with pytest.raises(ValueError):
        re_mod.normalize_github_url("not a repo at all, just words")


def test_normalize_github_url_rejects_non_github_host():
    with pytest.raises(ValueError):
        re_mod.normalize_github_url("https://gitlab.com/pallets/flask")


# ---- is_probably_secret -------------------------------------------------

@pytest.mark.parametrize(
    "filename",
    [".env", ".env.production", "credentials.json", "secrets.yaml",
     "id_rsa", "id_ed25519.pub", ".npmrc", "server.pem", "site.key"],
)
def test_is_probably_secret_flags_known_secret_files(tmp_path, filename):
    f = tmp_path / filename
    f.write_text("super-secret-value")
    assert re_mod.is_probably_secret(f) is True


@pytest.mark.parametrize("filename", ["README.md", "main.py", "package.json", "app.env.md"])
def test_is_probably_secret_leaves_normal_files_alone(tmp_path, filename):
    f = tmp_path / filename
    f.write_text("normal content")
    assert re_mod.is_probably_secret(f) is False


def test_is_probably_text_excludes_secrets_even_if_not_binary(tmp_path):
    # A .env file is plain text, not binary -- must still be excluded because
    # of its name, not its content.
    f = tmp_path / ".env"
    f.write_text("ANTHROPIC_API_KEY=sk-ant-fake-value-for-test")
    assert re_mod.is_probably_text(f) is False


def test_is_probably_text_excludes_binary_files(tmp_path):
    f = tmp_path / "image.png"
    f.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)
    assert re_mod.is_probably_text(f) is False


def test_is_probably_text_accepts_plain_source_files(tmp_path):
    f = tmp_path / "main.py"
    f.write_text("print('hello world')\n")
    assert re_mod.is_probably_text(f) is True


# ---- build_file_tree -----------------------------------------------------

def test_build_file_tree_skips_noise_directories(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("noise")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main")

    tree = re_mod.build_file_tree(tmp_path)

    assert "main.py" in tree
    assert "src/" in tree
    assert "node_modules" not in tree
    assert ".git" not in tree


def test_build_file_tree_respects_max_entries(tmp_path):
    for i in range(10):
        (tmp_path / f"file_{i}.txt").write_text("x")

    tree = re_mod.build_file_tree(tmp_path, max_entries=3)

    assert "truncated" in tree
    assert len(tree.splitlines()) == 4  # 3 entries + the truncation notice


# ---- collect_file_contents ------------------------------------------------

def test_collect_file_contents_prioritizes_readme_over_random_files(tmp_path):
    (tmp_path / "zzz_random.txt").write_text("not important")
    (tmp_path / "README.md").write_text("# Project\nThis is the readme.")

    files = re_mod.collect_file_contents(tmp_path)
    rels = [rel for rel, _ in files]

    assert rels[0] == "README.md"


def test_collect_file_contents_never_includes_secret_files(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-should-not-leak")
    (tmp_path / "README.md").write_text("# Project")

    files = re_mod.collect_file_contents(tmp_path)
    rels = [rel for rel, _ in files]

    assert ".env" not in rels
    assert not any("sk-ant-should-not-leak" in content for _, content in files)


def test_collect_file_contents_respects_max_files(tmp_path):
    for i in range(20):
        (tmp_path / f"module_{i}.py").write_text(f"# module {i}")

    files = re_mod.collect_file_contents(tmp_path, max_files=5)

    assert len(files) <= 5


def test_collect_file_contents_respects_char_budget(tmp_path):
    # Each file is ~50 chars; a budget of 120 should cap us at 2-3 files.
    for i in range(10):
        (tmp_path / f"module_{i}.py").write_text("x" * 45 + f"_{i}")

    files = re_mod.collect_file_contents(tmp_path, max_files=100, max_total_chars=120)
    total_chars = sum(len(content) for _, content in files)

    assert total_chars <= 120


# ---- build_prompt ----------------------------------------------------------

def test_build_prompt_includes_word_limit_and_repo_url():
    prompt = re_mod.build_prompt(
        repo_url="https://github.com/pallets/flask.git",
        tree="README.md",
        files=[("README.md", "hello")],
        word_limit=200,
    )
    assert "https://github.com/pallets/flask.git" in prompt
    assert "under 200 words" in prompt


# ---- agent tools (--agent mode) --------------------------------------------
# These are the tools handed to the model in agentic mode. Since the model
# supplies the paths, path-traversal safety here is the highest-risk surface
# in the whole project -- a malicious or confused "../../etc/passwd" request
# must never escape the cloned repo directory.

def test_resolve_within_root_allows_normal_relative_paths(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("x = 1")

    resolved = re_mod._resolve_within_root(tmp_path, "src/main.py")

    assert resolved == (tmp_path / "src" / "main.py").resolve()


@pytest.mark.parametrize(
    "escape_attempt",
    ["../../etc/passwd", "../outside.txt", "src/../../../etc/passwd"],
)
def test_resolve_within_root_blocks_path_traversal(tmp_path, escape_attempt):
    # These use enough '../' to walk past the root even after the leading-
    # slash strip, so they must be rejected outright.
    assert re_mod._resolve_within_root(tmp_path, escape_attempt) is None


def test_resolve_within_root_treats_leading_slash_as_repo_relative(tmp_path):
    # A model-supplied path starting with '/' is NOT treated as an absolute
    # filesystem path -- the leading slash is stripped and it's resolved
    # relative to the repo root instead. That keeps it safely contained;
    # it just means "/etc/passwd" resolves to "<root>/etc/passwd" (which
    # won't exist) rather than escaping to the real /etc/passwd.
    resolved = re_mod._resolve_within_root(tmp_path, "/etc/passwd")
    assert resolved is not None
    assert resolved.is_relative_to(tmp_path.resolve())


def test_tool_list_directory_lists_root(tmp_path):
    (tmp_path / "README.md").write_text("# hi")
    (tmp_path / "src").mkdir()

    result = re_mod.tool_list_directory(tmp_path, ".")

    assert "README.md" in result
    assert "src/" in result


def test_tool_list_directory_skips_noise_dirs(tmp_path):
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "README.md").write_text("# hi")

    result = re_mod.tool_list_directory(tmp_path, ".")

    assert "node_modules" not in result
    assert "README.md" in result


def test_tool_list_directory_rejects_path_escape(tmp_path):
    result = re_mod.tool_list_directory(tmp_path, "../../etc")
    assert "Error" in result
    assert "not allowed" in result


def test_tool_list_directory_reports_missing_path(tmp_path):
    result = re_mod.tool_list_directory(tmp_path, "does_not_exist")
    assert "Error" in result


def test_tool_read_file_returns_contents(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')")

    result = re_mod.tool_read_file(tmp_path, "main.py")

    assert result == "print('hello')"


def test_tool_read_file_refuses_secret_files(tmp_path):
    (tmp_path / ".env").write_text("ANTHROPIC_API_KEY=sk-ant-should-not-leak")

    result = re_mod.tool_read_file(tmp_path, ".env")

    assert "Refused" in result
    assert "sk-ant-should-not-leak" not in result


def test_tool_read_file_refuses_path_escape(tmp_path):
    result = re_mod.tool_read_file(tmp_path, "../../etc/passwd")
    assert "Error" in result
    assert "not allowed" in result


def test_tool_read_file_truncates_long_files(tmp_path):
    f = tmp_path / "big.py"
    f.write_text("x" * (re_mod.MAX_CHARS_PER_FILE + 500))

    result = re_mod.tool_read_file(tmp_path, "big.py")

    assert len(result) <= re_mod.MAX_CHARS_PER_FILE + len("\n... (truncated)")
    assert result.endswith("... (truncated)")


def test_tool_read_file_reports_directory_instead_of_file(tmp_path):
    (tmp_path / "src").mkdir()
    result = re_mod.tool_read_file(tmp_path, "src")
    assert "directory" in result.lower()
