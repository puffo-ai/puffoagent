"""Thin entry point that dispatches to the multi-agent portal CLI.

Historical usage (``python main.py``) is still supported, but the
daemon is now the multi-agent portal rather than a single hard-wired
agent. ``python main.py start`` runs the daemon; ``python main.py
agent create ...`` registers agents; see ``python main.py --help``.
"""

from __future__ import annotations

import os
import sys


def main() -> int:
    # The preferred way to run the portal is `pip install -e .` + the
    # `puffoagent` console script. This shim is a convenience for folks
    # who clone the repo and just want to try it without installing:
    # it inserts ./src on sys.path so `import puffoagent.portal.cli`
    # resolves to the package next to this file.
    here = os.path.dirname(os.path.abspath(__file__))
    src_dir = os.path.join(here, "src")
    if os.path.isdir(src_dir) and src_dir not in sys.path:
        sys.path.insert(0, src_dir)

    from puffoagent.portal.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
