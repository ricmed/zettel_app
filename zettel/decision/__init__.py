"""Typed decision layer (TypeSafe Jev), shadow mode only (ADR-055).

The decision model answers typed questions -- ``noul`` (yes/no probability),
``choice`` (one of N labelled options) and ``score`` (position on an ordered
scale) -- with probabilities instead of generated text. Here it runs *beside*
decisions the pipeline already takes and records what it would have decided;
nothing branches on its answer.

Layout:

* ``client.py``  -- the one place that talks to the SDK; fail-open.
* ``permute.py`` -- order-rotated copies of each ``choice`` and their aggregate.
* ``sites.py``   -- pure builders: decision inputs -> (state, questions).
* ``shadow.py``  -- per-site hooks called by the pipeline; persist to SQLite.

Siblings import each other by absolute path; nothing is re-exported here.
"""
