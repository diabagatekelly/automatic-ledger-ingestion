"""Guard the Cloud Functions deploy contract.

The function is deployed with ``--source=. --entry-point=webhook``, so the
Cloud Functions buildpack imports the entry point from a *top-level* ``main.py``.
The application lives under ``src/``; the root ``main.py`` is a shim that must
re-export the real handler. This test fails if that wiring is ever broken.
"""

import importlib

import src.main


def test_root_main_reexports_src_webhook() -> None:
    root_main = importlib.import_module("main")
    assert root_main.webhook is src.main.webhook
