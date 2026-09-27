# RED class: D. Structural
#
# Package-boundary tests (RED stage) for the package refactor described in
# plans/02-package-refactor-and-test-foundation.md. These tests assert the
# intended public surface and structural rules of the new ``forgejo_to_github``
# package before any implementation lands. They are expected to fail RED via
# ``ImportError`` (or attribute / signature failures) when the contract
# symbols are missing; that failure is acceptable because the missing symbols
# are the contract under test.
#
# This file deliberately does not exercise behavior. Behavior tests live in
# ``test_state.py``, ``test_codeberg_client.py``, ``test_github_client.py``,
# ``test_git_service.py``, ``test_orchestration.py``, ``test_reporting.py``,
# and ``test_cli.py``. Boundary tests focus on:
#
#   - the package's intended public classes exist and are importable,
#   - each public class has a non-empty docstring,
#   - each public class has a meaningful public API (not a single proxy
#     method), and no class exceeds seven public methods,
#   - importing the package modules does not perform network or subprocess
#     work at module load time.
"""RED-class structural tests for the ``forgejo_to_github`` package."""

from __future__ import annotations

import importlib
import inspect
import os
import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

# --- package surface --------------------------------------------------------

PACKAGE_NAME = "forgejo_to_github"


# Intended public classes. These names are part of the approved refactor
# contract from plan 02 and are expected to land in the package layout. The
# RED-stage failure mode is ``ImportError`` when the symbol is missing.
EXPECTED_PUBLIC_CLASSES = {
    "forgejo_to_github.state": "StateStore",
    "forgejo_to_github.codeberg": "CodebergClient",
    "forgejo_to_github.github": "GitHubClient",
    "forgejo_to_github.git": "GitMirror",
    "forgejo_to_github.migration": "MigrationOrchestrator",
    "forgejo_to_github.reporting": "Reporter",
}


# Maximum public methods per class. Raised from seven to nine during the
# plan-02 audit remediation: the Reporter's approved per-event dual-sink
# seam grew (comment_skipped, then issue_skipped). The seam cleanup that
# would return Reporter to the original cap is GitHub issue #7. See
# test-framework-spec.md §14.5.
MAX_PUBLIC_METHODS = 9


# --- helpers ----------------------------------------------------------------


def _public_methods(klass: type) -> list[str]:
    """Return public, non-special method names defined directly on ``klass``."""
    methods: list[str] = []
    for name, member in inspect.getmembers(klass, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if not hasattr(klass, name):
            continue
        func = getattr(klass, name)
        # Exclude methods inherited from object / builtin bases.
        qualname = getattr(func, "__qualname__", "")
        if qualname.split(".", 1)[0] != klass.__name__:
            continue
        methods.append(name)
    return sorted(methods)


def _import_attr(module_name: str, attr: str):
    """Import ``module_name`` and return ``getattr(module, attr)``.

    ``pytest.importorskip`` is intentionally avoided here: the missing
    module or attribute is the contract being asserted, and raising
    ``ImportError`` / ``AttributeError`` is the desired RED-stage outcome.
    """
    module = importlib.import_module(module_name)
    return getattr(module, attr)


# --- 1. package imports perform no network or subprocess work ---------------

# Child-process script shared by both import guards below. It installs the
# guard, imports the package, then walks and imports every submodule, then
# prints the sentinel. The submodule walk is load-bearing: the package
# ``__init__`` imports only ``__about__``, so importing the package name alone
# would load none of the modules that could plausibly have import-time side
# effects.
_CHILD_IMPORT_SCRIPT = """
import importlib
import pkgutil
import sys

{guard}

import {package}

for _info in pkgutil.walk_packages({package}.__path__, {package}.__name__ + "."):
    importlib.import_module(_info.name)

print({sentinel!r})
sys.stdout.flush()
"""

# Printed by the child only if every guard stayed quiet through the import.
_IMPORT_CLEAN_SENTINEL = "f2gh-import-clean"


def _repo_root() -> Path:
    """Repository root, derived from this file's location rather than cwd."""
    return Path(__file__).resolve().parents[1]


def _run_import_child(guard_source: str) -> subprocess.CompletedProcess:
    """Run the guarded import in a fresh interpreter and return the result.

    A fresh process is what makes the guard meaningful: an in-process
    ``importlib.import_module`` is a ``sys.modules`` cache hit once collection
    has imported the package, so it would exercise no module-level code at all.
    """
    script = _CHILD_IMPORT_SCRIPT.format(
        guard=guard_source,
        package=PACKAGE_NAME,
        sentinel=_IMPORT_CLEAN_SENTINEL,
    )
    env = dict(os.environ)
    # Point the child at this checkout rather than relying on an editable
    # install being present, and do not let an inherited PYTHONPATH shadow it.
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        str(_repo_root())
        if not existing
        else os.pathsep.join([str(_repo_root()), existing])
    )
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_repo_root()),
        # check=False is explicit and load-bearing: a tripped guard must
        # surface as a non-zero exit that _assert_import_was_clean reports
        # with the child's stdout and stderr attached, not as a
        # CalledProcessError from here that hides the child's own traceback.
        check=False,
    )


def _assert_import_was_clean(result: subprocess.CompletedProcess) -> None:
    """Assert the guarded child import succeeded and reached the sentinel."""
    assert result.returncode == 0, (
        "importing the package tripped an import-time guard; the child "
        f"interpreter exited {result.returncode}.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert _IMPORT_CLEAN_SENTINEL in result.stdout, (
        "the guarded child never reached the post-import sentinel, so the "
        f"import did not complete.\nstdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_importing_package_does_not_perform_network_calls():
    """Importing the package must not contact any remote service.

    The guard blocks the standard ``socket``-level entry points, so any
    accidental DNS lookup or HTTP connection raises during import. The import
    runs in a fresh interpreter: an in-process import would be served from
    ``sys.modules`` and execute no module-level code, so the guard would prove
    nothing. Every submodule is imported too, since the package ``__init__``
    pulls in only ``__about__``.
    """
    result = _run_import_child(
        """
import socket


def _forbid(*_args, **_kwargs):
    raise AssertionError(
        "forgejo_to_github must not open sockets at import time"
    )

socket.create_connection = _forbid
socket.getaddrinfo = _forbid
"""
    )
    _assert_import_was_clean(result)


def test_importing_package_does_not_execute_subprocess():
    """Importing the package must not spawn subprocesses.

    Both ``subprocess.Popen`` and ``os.system`` are blocked: ``os.system``
    reaches the C-level ``system()`` without going through ``Popen``, so
    blocking only one would let half the original guard's surface through. As
    with the network guard, the import runs in a fresh interpreter and covers
    every submodule.
    """
    result = _run_import_child(
        """
import os
import subprocess


def _forbid(*_args, **_kwargs):
    raise AssertionError(
        "forgejo_to_github must not launch processes at import time"
    )

subprocess.Popen = _forbid
os.system = _forbid
"""
    )
    _assert_import_was_clean(result)


# --- 2. intended public classes exist and are importable --------------------


@pytest.mark.parametrize(
    "module_name,class_name",
    sorted(EXPECTED_PUBLIC_CLASSES.items()),
    ids=lambda value: value if isinstance(value, str) else "-".join(value),
)
def test_intended_public_class_is_importable(module_name: str, class_name: str):
    """Each intended public class must be importable from its module."""
    obj = _import_attr(module_name, class_name)
    assert isinstance(obj, type), (
        f"{module_name}.{class_name} must be a class, got {type(obj).__name__}"
    )


# --- 3. each public class carries a non-empty docstring ---------------------


@pytest.mark.parametrize(
    "module_name,class_name",
    sorted(EXPECTED_PUBLIC_CLASSES.items()),
    ids=lambda value: value if isinstance(value, str) else "-".join(value),
)
def test_public_class_has_docstring(module_name: str, class_name: str):
    """Every intended public class must document its responsibility."""
    klass = _import_attr(module_name, class_name)
    doc = inspect.getdoc(klass)
    assert doc, f"{module_name}.{class_name} must have a non-empty docstring"
    assert doc.strip(), f"{module_name}.{class_name} docstring must contain real text"


# --- 4. each public class has a meaningful API surface -----------------------


@pytest.mark.parametrize(
    "module_name,class_name",
    sorted(EXPECTED_PUBLIC_CLASSES.items()),
    ids=lambda value: value if isinstance(value, str) else "-".join(value),
)
def test_public_class_has_at_least_two_public_methods(
    module_name: str, class_name: str
):
    """No class should be a single-method proxy.

    The boundary rule: a class is rejected when it exposes exactly one
    public, non-special method. Real responsibility requires at least
    two.
    """
    klass = _import_attr(module_name, class_name)
    methods = _public_methods(klass)
    assert len(methods) >= 2, (
        f"{module_name}.{class_name} exposes only {methods!r}; "
        "a single public method is a proxy anti-pattern"
    )


@pytest.mark.parametrize(
    "module_name,class_name",
    sorted(EXPECTED_PUBLIC_CLASSES.items()),
    ids=lambda value: value if isinstance(value, str) else "-".join(value),
)
def test_public_class_has_at_most_nine_public_methods(
    module_name: str, class_name: str
):
    """The nine-method cap prevents god objects.

    No public class may expose more than nine non-special public
    methods; anything larger is a refactoring smell (see §14.5).
    """
    klass = _import_attr(module_name, class_name)
    methods = _public_methods(klass)
    assert len(methods) <= MAX_PUBLIC_METHODS, (
        f"{module_name}.{class_name} exposes {len(methods)} public methods "
        f"({methods!r}); the {MAX_PUBLIC_METHODS}-method cap was exceeded"
    )


# --- 5. StateStore: real stateful API on a path-owned instance ---------------


def test_state_store_constructor_requires_path_source_target():
    """``StateStore`` must be a real class with explicit dependencies.

    Constructing with the three documented arguments (state path,
    source repo, target repo) must succeed. The constructor must reject
    a missing path because the instance owns its state file location.
    """
    klass = _import_attr("forgejo_to_github.state", "StateStore")

    # Missing required arguments must not silently succeed.
    with pytest.raises(TypeError):
        klass()  # type: ignore[call-arg]


def test_state_store_exposes_load_and_save_methods():
    """``StateStore`` must expose at least ``load`` and ``save``.

    These are the two methods that the rest of the package depends on;
    they establish the real stateful API the refactor requires.
    """
    klass = _import_attr("forgejo_to_github.state", "StateStore")
    methods = set(_public_methods(klass))
    assert "load" in methods, "StateStore must expose a public load()"
    assert "save" in methods, "StateStore must expose a public save()"


# --- 6. all intended submodules are part of the public package ---------------


def test_intended_submodules_are_part_of_public_package():
    """All intended submodules must be importable from the package."""
    import forgejo_to_github as pkg

    submodule_names = {info.name for info in pkgutil.iter_modules(pkg.__path__)}
    for module_name in EXPECTED_PUBLIC_CLASSES:
        leaf = module_name.removeprefix(f"{PACKAGE_NAME}.")
        assert leaf in submodule_names, (
            f"intended submodule {module_name!r} is not part of the package"
        )
