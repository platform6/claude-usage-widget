# Repository Guidelines

## Project Structure & Module Organization

`claude_usage/` contains the Python package: `cli.py` handles commands, `collector.py` gathers usage, `config.py` manages settings, and `widget.py`/`overlay.py` implement the Qt interface. Custom rendering lives in `skins/`, palettes in `themes.py`, and packaged SVG assets in `icons/`. `main.py` is a local entry point. Tests live in `tests/test_*.py`; documentation, screenshots, and developer utilities live in `docs/`, `screenshots/`, and `scripts/`. `Formula/` contains Homebrew packaging.

## Build, Test, and Development Commands

Use Python 3.10+ and a virtual environment.

- `python -m pip install -e . pytest`: install the editable package and test runner.
- `claude-usage` or `python main.py`: launch the widget locally.
- `QT_QPA_PLATFORM=offscreen python -m pytest -q`: run the headless suite in Bash, matching CI.
- In PowerShell, set `$env:QT_QPA_PLATFORM = "offscreen"`, then run `python -m pytest -q`.
- `python -m pytest tests/test_config.py -q`: run a focused test module after setting the Qt environment variable.
- `python -m pip install build`, then `python -m build`: generate source and wheel distributions in `dist/`.

## Coding Style & Naming Conventions

Follow `.editorconfig`: UTF-8, LF line endings, final newlines, and four-space Python indentation. Use `snake_case` for modules/functions, `PascalCase` for classes, and `UPPER_CASE` for constants. Match surrounding type hints and docstrings; explain why in comments. Use English throughout. No formatter or linter is enforced. Keep runtime dependencies limited to PySide6-Essentials, certifi, and the standard library.

## Testing Guidelines

Pytest runs the suite, including `unittest.TestCase` tests. Name files `test_<module>.py` and methods/functions `test_<behavior>`. Add tests for new behavior and regression tests that fail before bug fixes. CI covers Python 3.10–3.12; no numeric coverage threshold is configured. Run the widget manually. For OSD rendering or toggles, verify all 11 themes: five classics and six skins with separate paint paths.

## Commit & Pull Request Guidelines

Follow recent commit subjects such as `fix(overlay): ...`, `docs(readme): ...`, or `build(homebrew): ...`. Keep one fix or feature per PR. Complete `.github/PULL_REQUEST_TEMPLATE.md`: explain what changed and why, report test results and manual verification, and link related issues. Include screenshots for visual changes when helpful.

## Security & Configuration

Never commit OAuth credentials or raw prompt data. Preserve redaction at serialization boundaries. Keep configuration defaults in `claude_usage/config.py` aligned with documented examples. Report vulnerabilities privately as described in `SECURITY.md`.
