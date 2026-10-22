"""Protocol compatibility is independent of product release numbers."""
from __future__ import annotations

from collections.abc import Mapping

SUPPORTED_CONTRACT_VERSIONS = frozenset({"0.0.1"})
MIN_REQUIRED_RECORD_MODEL = 1


class CompatibilityError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def check_remote_health(
    health: Mapping[str, object], *, required_record_model: int = MIN_REQUIRED_RECORD_MODEL,
) -> int:
    if isinstance(required_record_model, bool) or required_record_model < 1:
        raise ValueError("required record model must be a positive integer")
    # Old health responses lack these optional declarations. Explicit invalid
    # declarations must never turn into an apparently compatible legacy peer.
    if "status" in health and health["status"] != "ok":
        raise CompatibilityError("REMOTE_INVALID_RESPONSE")
    contract = health.get("contract_version", "0.0.1")
    if not isinstance(contract, str) or not contract:
        raise CompatibilityError("REMOTE_INVALID_RESPONSE")
    if contract not in SUPPORTED_CONTRACT_VERSIONS:
        raise CompatibilityError("REMOTE_PROTOCOL_INCOMPATIBLE")
    model = health.get("record_model_version", 1)
    if isinstance(model, bool) or not isinstance(model, int) or model < 1:
        raise CompatibilityError("REMOTE_INVALID_RESPONSE")
    if model < required_record_model:
        raise CompatibilityError("REMOTE_RECORD_MODEL_TOO_OLD")
    return model
