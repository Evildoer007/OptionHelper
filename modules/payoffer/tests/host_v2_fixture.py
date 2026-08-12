"""Payoffer测试使用的ModuleHost v2授权上下文。"""

from __future__ import annotations

import time

from runtime.protocol.models import CallerContext
from runtime.protocol.module_host import (
    HostObjectRef,
    ModuleHostContext,
    issue_capability_token,
    verify_module_host_context,
)


TOKEN_SECRET = b"payoffer-module-host-v2-test-secret"
SESSION_ID = "session-payoffer-test"
PRINCIPAL_ID = "principal-payoffer-test"
AUDIENCE = "option-helper-app"


def authorized_context(
    *,
    request_policy: tuple[str, ...],
    task_id: str | None = None,
    analysis_case_id: str | None = None,
    candidate_id: str | None = None,
    catalog_version: str | None = None,
    contract_fingerprint: str | None = None,
    contract_ref: HostObjectRef | None = None,
) -> tuple[CallerContext, ModuleHostContext]:
    """签发并验证相互匹配的CallerContext与ModuleHostContext。"""
    expires_at = int(time.time()) + 300
    fields = {
        "session_ref": "local:payoffer-test-session",
        "module": "payoffer",
        "expires_at": expires_at,
        "context_id": "mhc_payoffer_test_0001",
        "page_hash": "2" * 64,
        "audience": AUDIENCE,
        "host_kind": "app",
        "request_policy": request_policy,
        "capability_version": "capability-test",
        "protocol_version": "protocol-test",
        "task_id": task_id,
        "analysis_case_id": analysis_case_id,
        "candidate_id": candidate_id,
        "catalog_version": catalog_version,
        "contract_fingerprint": contract_fingerprint,
        "contract_ref": contract_ref,
    }
    token = issue_capability_token(
        token_secret=TOKEN_SECRET,
        session_id=SESSION_ID,
        principal_id=PRINCIPAL_ID,
        **fields,
    )
    context = ModuleHostContext(
        capability_token=token,
        **{key: value for key, value in fields.items() if key != "expires_at" and key != "audience"},
    )
    verify_module_host_context(
        context,
        token_secret=TOKEN_SECRET,
        session_id=SESSION_ID,
        principal_id=PRINCIPAL_ID,
        audience=AUDIENCE,
    )
    caller = CallerContext(
        tenant_id="tenant_host",
        principal_id=PRINCIPAL_ID,
        role="test",
        capabilities=request_policy,
        session_id=SESSION_ID,
        audience=AUDIENCE,
        request_id="request-payoffer-test",
    )
    return caller, context
