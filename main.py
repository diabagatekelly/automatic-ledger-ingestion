"""Deployment entry point for Google Cloud Functions (Gen2).

The Cloud Functions Python buildpack imports the entry-point function from a
top-level ``main.py``. The application itself lives under ``src/`` (see
``src/main.py``); this shim re-exports the ``webhook`` handler so the deploy
command can use ``--source=. --entry-point=webhook`` while keeping the ``src``
package layout the tests import from.
"""

from src.main import webhook  # noqa: F401  (re-exported as the Cloud Function entry point)
