"""Regression contracts for the restrained OptionHelper app-tile finish."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import unittest
from xml.etree import ElementTree


ROOT = Path(__file__).resolve().parents[3]
SVG = ROOT / "assets" / "icons" / "optionhelper-app-icon-tile-light.svg"
NAMESPACE = {"svg": "http://www.w3.org/2000/svg"}


def _geometry_fingerprint(element: ElementTree.Element) -> str:
    """Hash SVG geometry without depending on ElementTree's global prefix map."""

    def canonical(node: ElementTree.Element) -> dict[str, object]:
        return {
            "tag": node.tag.rsplit("}", 1)[-1],
            "attributes": sorted(node.attrib.items()),
            "text": (node.text or "").strip(),
            "children": [canonical(child) for child in node],
        }

    payload = json.dumps(
        canonical(element),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class AppIconSurfaceFinishTests(unittest.TestCase):
    def test_tile_finish_is_a_subtle_center_light_material(self) -> None:
        root = ElementTree.parse(SVG).getroot()
        tile_gradient = root.find("svg:defs/svg:linearGradient[@id='tile']", NAMESPACE)
        sheen = root.find("svg:defs/svg:radialGradient[@id='tileSheen']", NAMESPACE)
        shadow = root.find("svg:defs/svg:filter[@id='tileShadow']/svg:feDropShadow", NAMESPACE)
        tile = root.find("svg:rect", NAMESPACE)
        highlights = root.findall("svg:rect", NAMESPACE)

        self.assertIsNotNone(tile_gradient)
        self.assertIsNotNone(sheen)
        self.assertIsNotNone(shadow)
        self.assertIsNotNone(tile)
        self.assertGreaterEqual(len(highlights), 3)
        self.assertEqual(
            [stop.attrib["stop-color"] for stop in tile_gradient.findall("svg:stop", NAMESPACE)],
            ["#FCFDFB", "#F1F2EF"],
        )
        self.assertEqual(
            {key: sheen.attrib[key] for key in ("cx", "cy", "r")},
            {"cx": "50%", "cy": "43%", "r": "86%"},
        )
        self.assertEqual(sheen.find("svg:stop", NAMESPACE).attrib["stop-opacity"], ".14")
        self.assertEqual(
            {key: shadow.attrib[key] for key in ("dx", "dy", "stdDeviation", "flood-opacity")},
            {"dx": "0", "dy": "8", "stdDeviation": "16", "flood-opacity": ".10"},
        )
        self.assertEqual(
            {key: tile.attrib[key] for key in ("stroke", "stroke-opacity", "stroke-width")},
            {"stroke": "#D2D4CF", "stroke-opacity": ".38", "stroke-width": "2.5"},
        )
        self.assertEqual(highlights[2].attrib["stroke-opacity"], ".48")

    def test_red_mark_geometry_is_untouched_by_surface_changes(self) -> None:
        root = ElementTree.parse(SVG).getroot()
        mark = root.find("svg:g", NAMESPACE)
        self.assertIsNotNone(mark)
        self.assertEqual(mark.attrib["transform"], "translate(208.4 208.4) scale(2.371875)")
        self.assertEqual(
            _geometry_fingerprint(mark),
            "c8f788b33223733cfe53ad3b5e4251e942ecb410b1afca5e660fcdb35ed05343",
        )


if __name__ == "__main__":
    unittest.main()
