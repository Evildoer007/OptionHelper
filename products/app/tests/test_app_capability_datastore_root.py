"""DataFetcher writes and compute modules read the same App-owned DataStore."""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
APP_ROOT = ROOT / "products" / "app"
CORE_SRC = ROOT / "core" / "src"
for source in (APP_ROOT, CORE_SRC):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from backend.app_server import AppServer
from backend.authorization.roles import Role
from backend.identity.session_identity import SessionIdentity
from capability_fixture import capability_root
from runtime.adapters.local_store import LocalDataStore
from runtime.bootstrap import bootstrap_runtime, release_runtime_scope


def test_capability_writer_and_app_bound_reader_share_one_external_store() -> None:
    with tempfile.TemporaryDirectory(prefix="optionhelper-store-closure-") as temporary:
        root = Path(temporary)
        unrelated_runtime = root / "unrelated-runtime"
        with patch.dict(os.environ, {"OPTIONHELPER_RUNTIME_ROOT": str(unrelated_runtime)}):
            app = AppServer(app_data_dir=root / "app-state", capability_root=capability_root())
            identity = SessionIdentity("principal-a", "tenant-a", Role.ADMIN, "session-a")

            with release_runtime_scope(app._capability_runtime_root):
                capability_source = app.registry.capability_root / "scripts" / "modules" / "datafetcher" / "service.py"
                writer = LocalDataStore(bootstrap_runtime(capability_source).data_root)
                reference = writer.put_bytes(
                    tenant_id=identity.tenant_id,
                    data_asset_id="market-a",
                    payload=b"date,close\n2026-08-11,100\n",
                    media_type="text/csv",
                    schema_id="market-history-v1",
                    asset_ids=("000905.SH",),
                    normalized_fields=("close",),
                    coverage={"start_date": "2026-08-11", "end_date": "2026-08-11"},
                    row_count=1,
                    created_by=identity.principal_id,
                )

            reader = app.datafetcher.bind_data_store(identity)
            assert reader.read_bytes(reference, tenant_id=identity.tenant_id) == b"date,close\n2026-08-11,100\n"
            assert writer.root == app._capability_runtime_root / "data"
            assert writer.root != unrelated_runtime / "data"
