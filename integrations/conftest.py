"""Make the ``cag_gate`` package importable when running ``pytest integrations``
regardless of the working directory or which pyproject pytest picks as rootdir."""

import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
