#!/usr/bin/env python3
"""Instagram downloader - run this file to open the menu, or see --help for command-line use."""

import sys

if sys.version_info < (3, 9):
    sys.exit("This program needs Python 3.9 or newer.")

from igdl.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
