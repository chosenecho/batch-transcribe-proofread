"""Discover stable boundaries without importing storage, SDKs or workers."""
from bili_asr.contracts.registry import CONTRACTS, Contract, catalog, contract

__all__ = ["CONTRACTS", "Contract", "catalog", "contract"]
