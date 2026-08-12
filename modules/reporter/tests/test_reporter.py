"""v1目录型Reporter接口的迁移拒绝回归。

完整v2端到端覆盖见test_reporter_run_refs.py。本文件保留旧测试入口名称，确保
历史调用不会静默退回到result_dir协议。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
for source in (PROJECT_ROOT / "core" / "src", PROJECT_ROOT / "modules" / "reporter" / "src"):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))

from modules.reporter.models import ReporterError, ReportRequest


class ReporterLegacyRequestTest(unittest.TestCase):
    def test_v1_result_directory_request_is_explicitly_rejected(self) -> None:
        request = {
            "schema": "optionhelper.report-request/v1",
            "task_id": "task-legacy",
            "report_level": "detailed",
            "module_runs": {"candidate": {"payoff": {"result_dir": "/tmp/legacy"}}},
        }
        with self.assertRaisesRegex(ReporterError, "旧字段|schema"):
            ReportRequest.from_mapping(request)


if __name__ == "__main__":
    unittest.main()
