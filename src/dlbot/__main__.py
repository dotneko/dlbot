"""Allow ``python -m dlbot`` (used by the worker subprocess in combined mode)."""

from . import main

if __name__ == "__main__":
    main()
