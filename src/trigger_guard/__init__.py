"""Pure CPU building blocks; importing this package does not import vLLM."""
from .core import MatcherState, TokenMatcher, TriggerRule, load_policy
from .egress import EgressGuard, EgressProgram
from .protocol import Publication

__version__ = "0.1.0"
__all__ = ["EgressGuard", "EgressProgram", "MatcherState", "Publication",
           "TokenMatcher", "TriggerRule", "load_policy"]
