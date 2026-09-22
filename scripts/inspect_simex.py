"""Diagnóstico local de geometrias; não altera entradas nem acessa AWS.

Uso::

    python scripts/inspect_simex.py simex_2024.zip relatorio.json
"""

import json
import sys
import zipfile
from collections import Counter
from pathlib import Path

import geopandas as gpd
from shapely import make_valid
from shapely.validation import explain_validity


def inspect(source: str | Path) -> list[dict]:
    """Conta geometrias ausentes, vazias e inválidas de cada GeoJSON do ZIP."""
    report = []
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if not name.lower().endswith(".geojson"):
                continue
            with archive.open(name) as stream:
                frame = gpd.read_file(stream)
            null = frame.geometry.isna()
            empty = frame.geometry.is_empty
            invalid = ~frame.geometry.is_valid & ~null & ~empty
            reasons = Counter()
            repairs = Counter()
            examples = []
            for index, geom in frame.loc[invalid].geometry.items():
                reason = explain_validity(geom)
                reasons[reason.split("[")[0]] += 1
                fixed = make_valid(geom)
                repairs[
                    f"{fixed.geom_type}; valid={fixed.is_valid}; empty={fixed.is_empty}"
                ] += 1
                if len(examples) < 5:
                    examples.append(
                        {
                            "feature_position_1_based": int(index) + 1,
                            "reason": reason,
                            "repaired_type": fixed.geom_type,
                        }
                    )
            item = {
                "file": name,
                "features": len(frame),
                "missing": int(null.sum()),
                "empty": int(empty.sum()),
                "invalid": int(invalid.sum()),
                "geometry_types": frame.geom_type.value_counts().to_dict(),
                "reasons": dict(reasons),
                "repair_results": dict(repairs),
                "examples": examples,
                "columns": list(frame.columns),
            }
            report.append(item)
            print(json.dumps(item, ensure_ascii=True), flush=True)
    return report


if __name__ == "__main__":
    result = inspect(sys.argv[1])
    Path(sys.argv[2]).write_text(
        json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
    )
