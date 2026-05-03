# -*- coding: utf-8 -*-
"""Firstrade-specific result types.

For now these alias the cross-broker dataclasses in :mod:`brokers.base`
because every Firstrade-shaped value already fits the generic schema.
Keeping the alias module makes it cheap to add Firstrade-only fields
later (e.g., margin / option / extended hours flags) without breaking
the generic surface that the API and agent tool consume.
"""

from src.brokers.base import (
    BrokerAccount,
    BrokerBalance,
    BrokerLoginResult,
    BrokerOrder,
    BrokerPosition,
    BrokerSnapshot,
    BrokerTransaction,
)

__all__ = [
    "BrokerAccount",
    "BrokerBalance",
    "BrokerLoginResult",
    "BrokerOrder",
    "BrokerPosition",
    "BrokerSnapshot",
    "BrokerTransaction",
]
