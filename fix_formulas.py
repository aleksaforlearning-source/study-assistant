"""Queues the formula pages of lectures uploaded before the formula worker existed.

Topics, statuses and notes stay as they are. The running server's worker then
re-reads the queued pages in the background; summaries keep the old text until
they are regenerated.

    python fix_formulas.py 9 8 7
"""

import sys

from math_pages import enqueue_math_pages

for source_id in map(int, sys.argv[1:]):
    print(f"source {source_id}: {enqueue_math_pages(source_id)} pages queued")
