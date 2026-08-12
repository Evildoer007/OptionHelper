"""跨模块稳定错误协议。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    CONTRACT_INVALID = "contract_invalid"
    DATA_UNAVAILABLE = "data_unavailable"
    UNAUTHORIZED = "unauthorized"
    UNSUPPORTED = "unsupported"
    CONFLICT = "conflict"
    CORRUPT = "corrupt"
    INTERNAL = "internal"


@dataclass(frozen=True)
class ErrorInfo:
    error_code: ErrorCode
    stage: str
    message: str
    retryable: bool = False
    missing_inputs: tuple[str, ...] = ()
    upstream_refs: tuple[str, ...] = ()

    @property
    def code(self) -> ErrorCode:
        """兼容首轮Core调用方；正式序列化字段固定为error_code。"""
        return self.error_code

    def to_dict(self) -> dict[str, object]:
        return {
            "error_code": self.error_code.value,
            "stage": self.stage,
            "message": self.message,
            "retryable": self.retryable,
            "missing_inputs": list(self.missing_inputs),
            "upstream_refs": list(self.upstream_refs),
        }


class RuntimeProtocolError(RuntimeError):
    def __init__(self, error: ErrorInfo) -> None:
        super().__init__(error.message)
        self.error = error


def error_info(error: Exception, *, stage: str = "runtime") -> ErrorInfo:
    from runtime.adapters.local_store import StoreIntegrityError
    from runtime.contracts.contract_api import ContractResolutionError, FormulaError

    if isinstance(error, RuntimeProtocolError):
        return error.error
    if isinstance(error, StoreIntegrityError):
        return ErrorInfo(ErrorCode.CORRUPT, stage, str(error))
    if isinstance(error, (ContractResolutionError, FormulaError)):
        return ErrorInfo(ErrorCode.CONTRACT_INVALID, stage, str(error))
    if isinstance(error, (ValueError, TypeError)):
        return ErrorInfo(ErrorCode.INVALID_REQUEST, stage, str(error))
    if isinstance(error, FileExistsError):
        return ErrorInfo(ErrorCode.CONFLICT, stage, str(error))
    if isinstance(error, PermissionError):
        return ErrorInfo(ErrorCode.UNAUTHORIZED, stage, str(error))
    if isinstance(error, FileNotFoundError):
        return ErrorInfo(ErrorCode.DATA_UNAVAILABLE, stage, str(error), retryable=True)
    return ErrorInfo(ErrorCode.INTERNAL, stage, "Core Runtime内部错误")


__all__ = ("ErrorCode", "ErrorInfo", "RuntimeProtocolError", "error_info")
