"""Package metadata: title, version, and summary.

Single source of truth for the release version. ``pyproject.toml`` reads
``__version__`` dynamically (``[tool.setuptools.dynamic]``), so bump the
version here — never in ``pyproject.toml`` directly.
"""

__title__: str = "f2gh"
__version__: str = "1.0.0"
__summary__: str = "Migrate repos from Codeberg/Forgejo to GitHub: issues, comments, labels, git mirror"

__all__ = ["__summary__", "__title__", "__version__"]
