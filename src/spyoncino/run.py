"""
Legacy compatibility wrapper.

`spyoncino` now points to the modular orchestrator in
`spyoncino.orchestrator_entrypoint`. This module is kept so existing
imports (`python -m spyoncino.run`) and the new `spyoncino-legacy`
command can continue to boot the classic synchronous stack.
"""

from .legacy.run import *  # noqa: F403
