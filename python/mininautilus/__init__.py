"""Python strategy interface to the authoritative Rust engine."""
from .bridge import Engine
from .strategy import TargetPosition

__all__ = ["Engine", "TargetPosition"]
