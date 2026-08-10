"""Tests for the Plugin.scroll_override hook (Scroll Manager Method 6)."""

from __future__ import annotations

from atlas.plugins.manager import Plugin, PluginManager


class _SilentPlugin(Plugin):
    name = "silent"


class _OverridingPlugin(Plugin):
    name = "overriding"

    def __init__(self, result: bool | None) -> None:
        self.result = result
        self.calls: list[tuple[object, int]] = []

    def scroll_override(self, container, pixels):
        self.calls.append((container, pixels))
        return self.result


class _ExplodingPlugin(Plugin):
    name = "exploding"

    def scroll_override(self, container, pixels):
        raise RuntimeError("boom")


def test_default_plugin_defers_scroll_override() -> None:
    """A plugin that never overrides this hook returns None (defer to the
    engine's generic scroll methods) - the correct default for most plugins."""
    manager = PluginManager()
    manager.register(_SilentPlugin())
    assert manager.scroll_override(container=object(), pixels=300) is None


def test_first_non_none_override_wins() -> None:
    manager = PluginManager()
    manager.register(_SilentPlugin())
    overriding = _OverridingPlugin(result=True)
    manager.register(overriding)
    result = manager.scroll_override(container="container", pixels=250)
    assert result is True
    assert overriding.calls == [("container", 250)]


def test_override_returning_false_is_still_authoritative() -> None:
    """False is a real answer (the plugin tried and it didn't move) - it must
    not be treated the same as None (defer)."""
    manager = PluginManager()
    manager.register(_OverridingPlugin(result=False))
    assert manager.scroll_override(container=object(), pixels=300) is False


def test_exploding_plugin_does_not_break_scroll_override() -> None:
    """A plugin hook that raises must never crash the scroll engine - the
    manager logs it and keeps checking the remaining plugins."""
    manager = PluginManager()
    manager.register(_ExplodingPlugin())
    overriding = _OverridingPlugin(result=True)
    manager.register(overriding)
    assert manager.scroll_override(container=object(), pixels=300) is True
