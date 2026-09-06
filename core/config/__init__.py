"""core.config — configuration module (defaults / schema / model)."""
from .defaults import DEFAULTS, _to_bool
from .schema import validate_config
from .model import PluginConfig

__all__ = ["DEFAULTS", "_to_bool", "validate_config", "PluginConfig"]
