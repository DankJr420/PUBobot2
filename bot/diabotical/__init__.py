# -*- coding: utf-8 -*-
"""Diabotical Wipeout result reporting for PUBobot2.

Importing this package makes :mod:`bot.diabotical.report` available as
``bot.diabotical.report`` (used by ``bot.match.match`` to start/stop the
background poller). The module imports are kept lightweight so that importing
the package during bot startup does not require a running event loop.
"""
from . import report  # noqa: F401  (exposed as bot.diabotical.report)
from . import client  # noqa: F401  (exposed as bot.diabotical.client)
