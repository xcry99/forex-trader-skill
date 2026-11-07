"""Shared core for the Forex day-trader skills.

Everything here is standard-library Python except :mod:`fx.gateway`, which
imports MetaTrader5 lazily so the rest of the pack (and the tests) run without
a terminal.
"""

__version__ = "1.0.0"
