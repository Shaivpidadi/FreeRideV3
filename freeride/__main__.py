"""Allow ``python -m freeride`` as an alternative to the ``freeride`` binary.

Useful when the user's PATH doesn't include the install location (common
when ``pip install --user`` puts the binary at ``~/.local/bin`` which
isn't on PATH on default macOS shells, or when the user forgot to
activate the venv they pip-installed into).
"""

import sys

from freeride.cli.main import main


if __name__ == "__main__":
    sys.exit(main())
