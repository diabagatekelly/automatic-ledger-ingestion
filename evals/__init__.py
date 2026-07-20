"""Live accuracy-eval harness for the Gemini parse (Issue #30).

Not part of the deployed Cloud Function (``src/``) and not run in the CI unit
gate — it makes real, nondeterministic, quota-costing Gemini calls. The pure
scoring logic lives in ``evals.scoring`` and IS unit-tested; the live runner is
``scripts/eval-gemini.py``.
"""
