"""Enable `python3 -m sumtag`.

Both this module and the installed `sumtag` console script funnel through
sumtag.cli:main, which returns an int exit code.
"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
