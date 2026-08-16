# Flask Web Framework – Code Report

## What this project does

Flask is a lightweight WSGI web application framework for Python. It provides the core infrastructure for building web applications—routing HTTP requests to Python functions, rendering templates, managing sessions, and handling configuration—without enforcing a particular project structure or requiring specific extensions. Developers use the `@app.route()` decorator to map URLs to view functions, and Flask handles the request/response cycle.

## Tech stack

- **Python 3.10+** (core language)
- **Werkzeug** ≥3.1.0 (WSGI utilities, routing, HTTP handling)
- **Jinja2** ≥3.1.2 (templating engine)
- **Click** ≥8.1.3 (CLI framework)
- **ItsDangerous** ≥2.2.0 (cryptographic signing for sessions)
- **Blinker** ≥1.9.0 (signals/events system)
- **MarkupSafe** ≥2.1.1 (HTML escaping)
- **Optional**: asgiref (async support), python-dotenv (environment files)
- **Dev/Test**: pytest, mypy, pyright, ruff, Sphinx (docs), tox

## Architecture / how it's organized

The codebase is split into several layers:

- **`src/flask/app.py`**: The main `Flask` class, which handles WSGI application logic, request dispatching, error handling, and context management.
- **`src/flask/sansio/`**: Sans-IO base classes (`App`, `Scaffold`, `Blueprint`) that contain logic without IO operations, designed for alternative implementations like Quart (async Flask).
- **`src/flask/ctx.py`**: Application and request context management. Recent refactoring merged `RequestContext` into `AppContext`.
- **`src/flask/blueprints.py`**: Blueprint system for modular applications (extends `sansio/blueprints.py`).
- **`src/flask/wrappers.py`**, **`helpers.py`**, **`globals.py`**: Request/response wrappers, utility functions, and global proxy objects (`request`, `session`, `g`).
- **`src/flask/json/`**: JSON handling with pluggable providers.
- **`src/flask/cli.py`**: Command-line interface built with Click.
- **`src/flask/templating.py`**, **`sessions.py`**, **`signals.py`**: Template rendering, cookie-based sessions, and event signals.
- **`tests/`**: Comprehensive pytest suite with fixtures in `conftest.py`.
- **`examples/`**: Tutorial blog app (`flaskr`), Celery integration, JavaScript/Ajax demos.
- **`docs/`**: Sphinx documentation source.

## Notable design choices or patterns

1. **Sans-IO separation**: The `sansio/` module isolates framework logic from IO, enabling alternative implementations (like Quart for async). The main `Flask` class inherits from `sansio.App` and adds WSGI-specific behavior.

2. **Context locals refactoring**: Version 3.2 merged `RequestContext` into `AppContext`, simplifying internal context tracking. Many `Flask` methods now accept an explicit `AppContext` parameter instead of relying solely on proxies—backwards compatibility is preserved with `remove_ctx`/`add_ctx` decorators that handle signature transitions.

3. **Decorator flexibility**: Template decorators (`@app.template_filter`, etc.) can now be used with or without parentheses (issue #5729), improving developer ergonomics.

## How to run it

**Install from source:**
```bash
git clone https://github.com/pallets/flask
cd flask
python3 -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -e .
```

**Run the tutorial example:**
```bash
cd examples/tutorial
pip install -e .
flask --app flaskr init-db
flask --app flaskr run --debug
# Visit http://127.0.0.1:5000
```

**Run tests:**
```bash
pip install -e '.[tests]'  # from repo root
pytest
```

The project uses `uv` for dependency management (see `uv.lock`) and tox for multi-Python testing.