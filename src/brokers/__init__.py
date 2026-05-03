# -*- coding: utf-8 -*-
"""Broker connectors namespace.

Each subpackage (``firstrade``, future ones) exposes a **read-only**
client that turns a brokerage's account / balance / position / order /
transaction views into the dataclasses defined in :mod:`brokers.base`.

Hard rules (enforced across every broker):
  * Never import a vendor's order/trade module. Only read-paths.
  * Never include credentials, cookies, tokens, or full account
    numbers in any output that leaves this package — strip them via
    :func:`brokers.base.redact_sensitive_payload` before the snapshot
    repository writes anything.
"""
