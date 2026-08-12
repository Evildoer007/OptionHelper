"""Secret只以不透明引用进入Provider。"""
from typing import Protocol

from runtime.protocol.models import SecretRef

class SecretRefProvider(Protocol):
    def resolve(self, secret_ref: SecretRef) -> str: ...

__all__ = ("SecretRefProvider",)
