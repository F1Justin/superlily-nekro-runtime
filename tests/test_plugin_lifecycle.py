import asyncio
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock

import pytest

from nekro_agent.core.config import config
from nekro_agent.services.plugin import collector as collector_module
from nekro_agent.services.plugin.base import NekroPlugin
from nekro_agent.services.plugin.collector import PluginCollector


def _plugin() -> NekroPlugin:
    plugin = NekroPlugin("Test", "lifecycle_test", "test", "1", "Test", "")
    plugin.init_method = AsyncMock()
    plugin.cleanup_method = AsyncMock()
    return plugin


async def _load(plugin: NekroPlugin, monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> PluginCollector:
    module = ModuleType("test_plugin_lifecycle")
    module.plugin = plugin
    monkeypatch.setattr(collector_module, "import_module", lambda _path: module)
    monkeypatch.setattr(config, "PLUGIN_ENABLED", [plugin.key] if enabled else [])
    collector = PluginCollector()
    await collector._load_plugin_module(module.__name__, Path("test_plugin_lifecycle.py"))
    return collector


async def test_disabled_plugin_is_registered_without_initializing_or_cleaning(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    plugin.init_method.side_effect = ConnectionError("optional service is offline")
    collector = await _load(plugin, monkeypatch, enabled=False)
    assert collector.get_plugin(plugin.key) is plugin
    assert not plugin.is_enabled
    assert not collector.failed_plugins
    await collector.cleanup_all_plugins()
    plugin.init_method.assert_not_awaited()
    plugin.cleanup_method.assert_not_awaited()


async def test_enabled_plugin_initializes_before_callback_and_cleans_once(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    enabled = AsyncMock()
    plugin.on_enabled()(enabled)
    collector = await _load(plugin, monkeypatch, enabled=True)
    assert plugin.is_enabled
    plugin.init_method.assert_awaited_once()
    enabled.assert_awaited_once()
    await collector.cleanup_all_plugins()
    await collector.cleanup_all_plugins()
    plugin.cleanup_method.assert_awaited_once()


async def test_hot_enable_initializes_once_and_disable_preserves_callback_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    disabled = AsyncMock()
    plugin.on_disabled()(disabled)
    await _load(plugin, monkeypatch, enabled=False)
    await plugin.enable()
    await plugin.enable()
    await plugin.disable()
    disabled.assert_awaited_once()
    await plugin.enable()
    plugin.init_method.assert_awaited_once()
    plugin.cleanup_method.assert_not_awaited()
    await plugin.cleanup()
    plugin.cleanup_method.assert_awaited_once()


async def test_failed_hot_enable_remains_disabled_and_can_retry(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    enabled = AsyncMock()
    plugin.on_enabled()(enabled)
    await _load(plugin, monkeypatch, enabled=False)
    plugin.init_method.side_effect = ConnectionError("offline")
    with pytest.raises(ConnectionError):
        await plugin.enable()
    assert not plugin.is_enabled
    enabled.assert_not_awaited()
    plugin.cleanup_method.assert_awaited_once()
    plugin.init_method.side_effect = None
    await plugin.enable()
    assert plugin.is_enabled
    assert plugin.init_method.await_count == 2
    enabled.assert_awaited_once()


async def test_failed_startup_is_reported_and_partial_resources_are_cleaned(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    plugin.init_method.side_effect = ConnectionError("offline")
    collector = await _load(plugin, monkeypatch, enabled=True)
    assert collector.get_plugin(plugin.key) is None
    assert collector.failed_plugins
    plugin.cleanup_method.assert_awaited_once()


async def test_concurrent_initialization_runs_once() -> None:
    plugin = _plugin()
    await asyncio.gather(plugin.initialize(), plugin.initialize())
    plugin.init_method.assert_awaited_once()


async def test_concurrent_hot_enable_triggers_callbacks_once(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin = _plugin()
    enabled = AsyncMock()
    plugin.on_enabled()(enabled)
    await _load(plugin, monkeypatch, enabled=False)

    async def initialize() -> None:
        await asyncio.sleep(0)

    plugin.init_method.side_effect = initialize
    await asyncio.gather(plugin.enable(), plugin.enable())
    plugin.init_method.assert_awaited_once()
    enabled.assert_awaited_once()


@pytest.mark.parametrize("enabled", [False, True])
async def test_unload_cleans_only_initialized_plugin(monkeypatch: pytest.MonkeyPatch, enabled: bool) -> None:
    plugin = _plugin()
    collector = await _load(plugin, monkeypatch, enabled=enabled)
    await collector.unload_plugin_by_module_name(plugin.module_name)
    assert collector.get_plugin(plugin.key) is None
    assert plugin.cleanup_method.await_count == int(enabled)
