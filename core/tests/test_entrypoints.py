from __future__ import annotations

import importlib.util
import contextlib
import io
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


def load_file(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EntrypointTest(unittest.TestCase):
    def test_tool_catalog_has_seven_internal_modules(self) -> None:
        entry = load_file("optionhelper_tool_entry", ROOT / "core" / "tool_entry.py")
        self.assertEqual(len(entry.tool_catalog()), 7)

    def test_page_catalog_has_five_operation_pages(self) -> None:
        host = load_file("optionhelper_module_host", ROOT / "core" / "module_host.py")
        self.assertEqual(set(host.page_catalog()), {"datafetcher", "payoffer", "pricer", "backtester", "reporter"})

    def test_module_host_reports_the_os_assigned_port(self) -> None:
        host = load_file("optionhelper_module_host", ROOT / "core" / "module_host.py")

        class BoundServer:
            def __init__(self, address: tuple[str, int], _handler: object) -> None:
                self.server_address = (address[0], 49321)
                self.server_port = 49321

        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            server = host._reported_server_class(BoundServer, "pricer")(("127.0.0.1", 0), object)
        announcement = output.getvalue()
        self.assertIn('"module": "pricer"', announcement)
        self.assertIn(f":{server.server_port}", announcement)
        self.assertNotIn(":0\"", announcement)


if __name__ == "__main__":
    unittest.main()
