"""外部数据、存储、凭据和模块宿主的抽象端口。"""

from .data_source import DataSourceProvider
from .data_store import DataStorePort, DataStoreReadPort
from .module import ModulePort
from .product_snapshot import ProductSnapshotProvider, require_product_snapshot_provider
from .result_selection import ResultSelectionPort, require_result_selection_port
from .result_store import ResultStorePort
from .secret_ref import SecretRefProvider
from .tool_gateway import ToolGatewayPort, require_tool_gateway_port

__all__ = (
    "DataSourceProvider", "DataStorePort", "DataStoreReadPort", "ModulePort", "ProductSnapshotProvider", "ResultSelectionPort", "ResultStorePort",
    "SecretRefProvider", "ToolGatewayPort", "require_product_snapshot_provider", "require_result_selection_port", "require_tool_gateway_port",
)
