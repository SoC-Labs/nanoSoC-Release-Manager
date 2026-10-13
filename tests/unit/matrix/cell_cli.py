"""The real `nanosoc-rm` CLI for the job-runner tests, with compat's test-only url_map seam bound from
$MATRIX_TEST_URL_MAP (a child process cannot receive it any other way). Used as jobs.Worker(argv_prefix=...)."""
import functools
import json
import os
import sys

from nanosoc_rm.matrix import cells

cells.run_cell = functools.partial(cells.run_cell, url_map=json.loads(os.environ["MATRIX_TEST_URL_MAP"]))
from nanosoc_rm.cli import main  # noqa: E402

sys.argv[0] = "nanosoc-rm"
main()
