"""Security/robustness tests for the plugin bootstrap (main.py).

The plugin imports its internals as top-level package names (core.*, webapi.*,
...) via a sys.path insertion. The AstrBot host purge only clears
"data.plugins.<name>.*" entries on reload, so main.py runs an eviction loop
that removes top-level modules whose __file__ resolves back into the plugin
directory. If that loop were broken or over-eager, either stale bytecode
survives every reload (invisible bug persistence) or host/astrbot modules get
evicted (breaks every other plugin on reload).

These tests exercise the eviction against the real sys.modules but restore
every entry afterwards, so the rest of the test session keeps its imports.

Run: pytest tests/security/test_bootstrap_eviction.py -v
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

PLUGIN_DIR = Path(__file__).resolve().parents[2]
PLUGIN_DIR_STR = str(PLUGIN_DIR)
TOP_LEVEL_PKGS = ("commands", "core", "webapi", "adapters", "ports", "bootstrap")


def _fake_module(name: str, file_path: str | None) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = file_path
    return mod


class TestModuleEviction:
    """Mirror of main.py's eviction loop; pins which modules must go."""

    def _evict(self) -> set[str]:
        """Run the same eviction main.py performs; return evicted keys."""
        evicted = set()
        for _pkg in TOP_LEVEL_PKGS:
            for _key in [k for k in sys.modules if k == _pkg or k.startswith(_pkg + ".")]:
                _mod = sys.modules.get(_key)
                _file = getattr(_mod, "__file__", None) or ""
                if _file and str(Path(_file).resolve()).startswith(PLUGIN_DIR_STR):
                    del sys.modules[_key]
                    evicted.add(_key)
        return evicted

    def _restore(self, snapshot: dict):
        sys.modules.update(snapshot)

    @staticmethod
    def _guarded_keys() -> list[str]:
        return [
            k for k in sys.modules
            if any(k == p or k.startswith(p + ".") for p in TOP_LEVEL_PKGS)
        ]

    def _snapshot_guarded(self) -> dict:
        """Snapshot every guarded top-level module (the eviction deletes real
        ones too — without a full snapshot the run pollutes later tests)."""
        return {k: sys.modules[k] for k in self._guarded_keys()}

    def test_plugin_modules_evicted(self):
        # Snapshot everything the eviction could touch — the injected fakes
        # plus any real plugin modules already imported by this session — so
        # the run is side-effect free for other tests.
        touched = self._guarded_keys()
        snapshot = {k: sys.modules[k] for k in touched}

        injected = [
            ("commands.handlers", str(PLUGIN_DIR / "commands" / "handlers.py")),
            ("bootstrap", str(PLUGIN_DIR / "bootstrap.py")),
        ]
        for name, path in injected:
            snapshot.pop(name, None)
            sys.modules[name] = _fake_module(name, path)
        try:
            evicted = self._evict()
        finally:
            self._restore(snapshot)
            for name, _ in injected:
                sys.modules.pop(name, None) if name not in snapshot else None

        # Every injected fake was evicted...
        assert {name for name, _ in injected} <= evicted
        # ...every evicted key belongs to a guarded top-level package...
        for key in evicted:
            assert any(key == p or key.startswith(p + ".") for p in TOP_LEVEL_PKGS), key
        # ...and every real plugin module visible in this session was evicted
        # too (a survivor would mean stale bytecode persists across reloads).
        for key in touched:
            f = getattr(snapshot.get(key), "__file__", None)
            if f and str(Path(f).resolve()).startswith(PLUGIN_DIR_STR):
                assert key in evicted, f"stale module survived: {key}"

    def test_same_named_modules_outside_plugin_dir_kept(self):
        # A foreign core.* / webapi.* (another plugin or site-packages) must
        # survive — the purge is path-scoped, not name-scoped.
        foreign = "/opt/other_plugin"
        kept = [
            ("core", f"{foreign}/core/__init__.py"),
            ("core.models", f"{foreign}/core/models.py"),
            ("webapi", f"{foreign}/webapi/__init__.py"),
        ]
        snapshot = self._snapshot_guarded()
        for name, path in kept:
            sys.modules[name] = _fake_module(name, path)
        try:
            evicted = self._evict()
        finally:
            self._restore(snapshot)
            for name, _ in kept:
                if name not in snapshot:
                    sys.modules.pop(name, None)
        # The foreign modules (same names, outside the plugin dir) survived;
        # anything else evicted is a real in-session plugin module, which is
        # expected behavior for this mirror of the loop.
        for name, _ in kept:
            assert name not in evicted, f"foreign module evicted: {name}"

    def test_host_namespace_modules_kept(self):
        # Modules without __file__ (namespace pkgs) must not crash the loop
        # nor be evicted.
        snapshot = self._snapshot_guarded()
        sys.modules["core"] = _fake_module("core", None)
        try:
            evicted = self._evict()
        finally:
            self._restore(snapshot)
            if "core" not in snapshot:
                sys.modules.pop("core", None)
        assert "core" not in evicted

    def test_real_units_module_resolves_to_plugin_dir(self):
        # The actual core.units source must live inside the plugin dir —
        # otherwise the eviction silently never fires and hot reload keeps
        # serving stale bytecode.
        spec = importlib.util.spec_from_file_location(
            "_evict_probe_units", PLUGIN_DIR / "core" / "units.py"
        )
        assert spec is not None and spec.origin is not None
        assert str(Path(spec.origin).resolve()).startswith(PLUGIN_DIR_STR)
