"""About-string generation for the ``f2gh`` CLI.

The single canonical string feeds both the ``ArgumentParser``
description and the ``--version`` output, so ``--help`` and
``--version`` always agree. A pure function (not a class): the
builder owns no state.
"""

from forgejo_to_github.__about__ import __summary__, __title__, __version__


def about() -> str:
    """Return the canonical ``title: summary version X`` string."""
    return f"{__title__}: {__summary__} version {__version__}"
