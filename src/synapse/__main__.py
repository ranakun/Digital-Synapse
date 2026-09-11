"""Enable `python -m synapse ...` as an entry point equivalent to the console script."""

from synapse.cli import app

if __name__ == "__main__":
    app()
