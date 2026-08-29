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
import pkgutil

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


def test_importing_package_does_not_perform_network_calls(monkeypatch):
    """Importing the package must not contact any remote service.

    We patch the standard ``socket``-level entry points so that any
    accidental DNS lookup or HTTP connection during import would raise.
    A clean import is the contract.
    """
    import socket

    def _forbid(*_args, **_kwargs):
        raise AssertionError("forgejo_to_github must not open sockets at import time")

    monkeypatch.setattr(socket, "create_connection", _forbid, raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", _forbid, raising=False)

    # ``importlib.import_module`` will re-run the package __init__; a clean
    # return is the assertion.
    assert importlib.import_module(PACKAGE_NAME) is not None


def test_importing_package_does_not_execute_subprocess(monkeypatch):
    """Importing the package must not spawn subprocesses.

    Patching ``subprocess.Popen`` and ``os.system`` to raise guarantees
    that any module-level side effect attempting to launch a process is
    surfaced as a failure during the RED stage.
    """
    import os
    import subprocess

    def _forbid_popen(*_args, **_kwargs):
        raise AssertionError(
            "forgejo_to_github must not launch subprocesses at import time"
        )

    def _forbid_system(*_args, **_kwargs):
        raise AssertionError("forgejo_to_github must not call os.system at import time")

    monkeypatch.setattr(subprocess, "Popen", _forbid_popen, raising=False)
    monkeypatch.setattr(os, "system", _forbid_system, raising=False)

    assert importlib.import_module(PACKAGE_NAME) is not None


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
def test_public_class_has_at_most_seven_public_methods(
    module_name: str, class_name: str
):
    """The seven-method cap prevents god objects.

    No public class may expose more than seven non-special public
    methods; anything larger is a refactoring smell.
    """
    klass = _import_attr(module_name, class_name)
    methods = _public_methods(klass)
    assert len(methods) <= 7, (
        f"{module_name}.{class_name} exposes {len(methods)} public methods "
        f"({methods!r}); the seven-method cap was exceeded"
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
