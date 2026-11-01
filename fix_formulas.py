"""Queues the formula and picture pages of files uploaded before the worker read them.

Pages already re-read are not queued again. Topics, statuses and notes stay as
they are; a described picture page joins the topics of the nearest page. The
running server's worker then re-reads the queued pages in the background;
summaries keep the old text until they are regenerated.

    python fix_formulas.py 9 8 7
"""

import sys

from math_pages import enqueue_vision_pages

for source_id in map(int, sys.argv[1:]):
    print(f"source {source_id}: {enqueue_vision_pages(source_id)} pages queued")
