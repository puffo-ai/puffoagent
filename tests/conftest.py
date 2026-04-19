"""Make pytest import the in-tree source tree rather than whatever
``puffoagent`` happens to be installed site-packages-wide. Lets the
test suite run against source without requiring a `pip install -e .`
(which would conflict with a running daemon's `.exe` on Windows).
"""

import sys
from pathlib import Path

_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
