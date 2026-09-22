"""Testes do reparo de geometrias da conversão web."""

import unittest

import geopandas as gpd
from shapely.geometry import Point, Polygon

from imazongeo_upload.web.conversion import repair_geometries


class RepairTests(unittest.TestCase):
    def test_repair_preserves_rows_and_attributes(self):
        frame = gpd.GeoDataFrame(
            {"name": ["a", "b"]},
            geometry=[
                Polygon([(0, 0), (2, 2), (0, 2), (2, 0), (0, 0)]),
                Polygon([(0, 0), (1, 0), (1, 1), (0, 0)]),
            ],
            crs="EPSG:4326",
        )
        result = repair_geometries(frame, "test.geojson")
        self.assertTrue(result.is_valid.all())
        self.assertEqual(result["name"].tolist(), ["a", "b"])
        self.assertEqual(result.crs, frame.crs)
        self.assertFalse(frame.is_valid.all())

    def test_missing_is_not_dropped(self):
        frame = gpd.GeoDataFrame(geometry=[None], crs="EPSG:4326")
        with self.assertRaisesRegex(ValueError, "ausentes"):
            repair_geometries(frame, "test.geojson")

    def test_non_polygon_is_not_dropped(self):
        frame = gpd.GeoDataFrame(geometry=[Point(0, 0)], crs="EPSG:4326")
        with self.assertRaisesRegex(ValueError, "componentes"):
            repair_geometries(frame, "test.geojson")
