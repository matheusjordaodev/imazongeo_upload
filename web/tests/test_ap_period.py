import sys
import unittest
from pathlib import Path
import geopandas as gpd
from shapely.geometry import Polygon
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from conversion import select_ap_period, merge_ap_period


def frame(values):
    return gpd.GeoDataFrame(values, geometry=[Polygon([(0,0),(1,0),(1,1),(0,0)])] * len(next(iter(values.values()))), crs='EPSG:4326')


class QuarterTests(unittest.TestCase):
    def test_filter_months(self):
        result = select_ap_period(frame({'ANO':[2025,2025,2024], 'MES':[1,4,4]}), 2025, 2)
        self.assertEqual(result['MES'].tolist(), [4])
        self.assertEqual(result['TRIMESTRE'].tolist(), [2])

    def test_replace_entire_quarter_preserves_other_periods(self):
        old = frame({'ANO':[2025,2025,2025,2024], 'MES':[4,5,1,4], 'id':[1,2,3,4]})
        new = select_ap_period(frame({'id':[5]}), 2025, 2)
        result = merge_ap_period(old, new, 2025, 2)
        self.assertEqual(result['id'].tolist(), [3,4,5])

    def test_missing_period_rejected(self):
        with self.assertRaises(ValueError):
            select_ap_period(frame({'ANO':[2025], 'MES':[1]}), 2025, 2)

    def test_ambiguous_history_rejected(self):
        with self.assertRaisesRegex(ValueError, 'Histórico'):
            merge_ap_period(frame({'id':[1]}), select_ap_period(frame({'id':[2]}), 2025, 2), 2025, 2)
