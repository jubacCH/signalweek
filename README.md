# signalweek

MVP built by the ai-company for opportunity #619.

## Toolchain

- Python **3.12** (single supported minor)
- [`uv`](https://docs.astral.sh/uv/) for environment and dependency management
- [`ruff`](https://docs.astral.sh/ruff/) for linting and formatting
- [`mypy`](https://mypy.readthedocs.io/) for static type checking (strict mode)
- [`pytest`](https://docs.pytest.org/) for testing

## Getting started

```bash
# Create the virtualenv and install dev dependencies
uv sync

# Run the tests
uv run pytest

# Lint
uv run ruff check .

# Type check
uv run mypy
```

## Layout

```
pyproject.toml   # project + tooling config
tests/           # pytest test suite (start with test_sanity.py)
```

## License

MIT — see [`LICENSE`](LICENSE).
