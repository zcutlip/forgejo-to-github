"""About-string generation for the ``f2gh`` CLI.

The single canonical string feeds the ``ArgumentParser``
description; ``--version`` prints the bare ``__version__``.
A pure function (not a class): the builder owns no state.
"""

from forgejo_to_github.__about__ import __summary__, __title__, __version__


def about() -> str:
    """Return the canonical ``title: summary version X`` string."""
    return f"{__title__}: {__summary__}. version {__version__}"
