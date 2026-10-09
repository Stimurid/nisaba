"""CLI entry point: `python -m nisaba [args...]` -> runs the archiver."""
from nisaba.archiver import main

if __name__ == "__main__":
    raise SystemExit(main())
