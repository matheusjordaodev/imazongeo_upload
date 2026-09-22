"""Testes da interface web (Flask) com o S3 simulado."""

import io
import json
import os
import time
import unittest
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from imazongeo_upload.web import server


@contextmanager
def capture_uploads() -> Iterator[dict[str, bytes]]:
    """Guarda o conteúdo de cada arquivo enviado ao S3 simulado, por chave."""
    captured: dict[str, bytes] = {}
    original = server.SimuladorS3.upload_file

    def capture(client, filename, bucket, key, **kwargs):
        captured[key] = Path(filename).read_bytes()
        return original(client, filename, bucket, key, **kwargs)

    with patch.object(server.SimuladorS3, "upload_file", capture):
        yield captured


class WebTests(unittest.TestCase):
    def sad_zip(
        self,
        properties=None,
        name="dados/alertas_sad_desmatamento_01_2025_municipios.geojson",
    ):
        content = {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": properties
                    if properties is not None
                    else {"ANO": 2025, "MES": 1, "MUNICIPIO": "Belem"},
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[-49, -2], [-48, -2], [-48, -1], [-49, -2]]],
                    },
                }
            ],
        }
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr(name, json.dumps(content))
        data.seek(0)
        return data, "sad.zip"

    def test_sad_converts_and_uploads_all_formats(self):
        with capture_uploads() as captured:
            result = self.finish(self.post(dataset="sad", files=self.sad_zip()))
        self.assertEqual(result["status"], "done", result["logs"])
        for fmt in ("geojson", "csv", "shapefile"):
            key = f"sad/{fmt}/sad_2025_01.zip"
            self.assertIn(key, captured)
            with zipfile.ZipFile(io.BytesIO(captured[key])) as archive:
                extensions = {Path(n).suffix for n in archive.namelist()}
                if fmt == "shapefile":
                    self.assertTrue(
                        {".shp", ".shx", ".dbf", ".prj"}.issubset(extensions)
                    )
                elif fmt == "csv":
                    self.assertIn("Belem", archive.read(archive.namelist()[0]).decode())
                else:
                    output = json.loads(archive.read(archive.namelist()[0]))
                    self.assertEqual(len(output["features"]), 1)
                    self.assertEqual(output["features"][0]["properties"]["ANO"], 2025)

    def test_sad_rejects_zip_without_geojson(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=self.sad_zip(name="alertas.shp")
            ).status_code,
            400,
        )

    def test_sad_rejects_invalid_attributes(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=self.sad_zip(properties={"ANO": 2025})
            ).status_code,
            400,
        )

    def test_sad_rejects_missing_period(self):
        self.assertEqual(
            self.post(dataset="sad", month="2", files=self.sad_zip()).status_code, 400
        )

    def test_sad_requires_zip(self):
        self.assertEqual(
            self.post(
                dataset="sad", files=(io.BytesIO(b"{}"), "sad.geojson")
            ).status_code,
            400,
        )

    def test_sad_accepts_arbitrary_filename(self):
        result = self.finish(
            self.post(
                dataset="sad",
                files=self.sad_zip(name="minha pasta/qualquer nome.geojson"),
                sad_type="degradacao",
                sad_layer="municipios",
            )
        )
        self.assertEqual(result["status"], "done", result["logs"])
        self.assertIn(
            "alertas_sad_degradacao_municipios.csv", "\n".join(result["logs"])
        )

    def test_sad_free_name_needs_classification(self):
        response = self.post(dataset="sad", files=self.sad_zip(name="livre.geojson"))
        self.assertEqual(response.status_code, 400)
        self.assertIn("tipo de alerta", response.json["error"])

    def setUp(self):
        # Independe de WEB_ALLOWED_HOSTS/WEB_PORT definidos no .env local
        hosts = patch.object(server, "ALLOWED_HOSTS", {"localhost:5000"})
        hosts.start()
        self.addCleanup(hosts.stop)
        self.client = server.app.test_client()
        self.client.get("/", base_url="http://localhost:5000")
        with self.client.session_transaction() as session:
            self.token = session["token"]

    def post(self, **changes):
        data = {
            "dataset": "floreser",
            "mode": "simulation",
            "operation": "download",
            "format": "csv",
            "year": "2025",
            "month": "1",
            "quarter": "1",
            "bucket": "imazongeo3-web",
            "files": self.sad_zip(),
            **changes,
        }
        return self.client.post(
            "/api/jobs",
            data=data,
            base_url="http://localhost:5000",
            headers={"X-CSRF-Token": self.token},
        )

    def finish(self, response):
        self.assertEqual(response.status_code, 202, response.json)
        for _ in range(200):
            result = self.client.get(
                "/api/jobs/" + response.json["id"], base_url="http://localhost:5000"
            )
            if result.json["status"] != "running" and not server.lock.locked():
                return result.json
            time.sleep(0.025)
        self.fail("Tempo limite excedido")

    def test_simulation_does_not_contact_aws(self):
        with patch(
            "imazongeo_upload.s3._novo_cliente_boto3",
            side_effect=AssertionError("AWS real"),
        ):
            result = self.finish(self.post())
        self.assertEqual(result["status"], "done")
        self.assertIn("floreser/csv/floreser_2025.csv", "\n".join(result["logs"]))

    def test_ap_dashboard_preview(self):
        result = self.finish(
            self.post(
                dataset="ameaca_pressao",
                operation="dashboard",
                format="geojson",
                mode="dry_run",
                files=self.sad_zip(name="ameaca_e_pressao_2025_geral_ameaca.geojson"),
            )
        )
        self.assertEqual(result["status"], "done")
        self.assertIn("DRY RUN", "\n".join(result["logs"]))

    def test_invalid_period(self):
        self.assertEqual(self.post(month="13").status_code, 400)

    def test_all_other_layers_generate_three_formats(self):
        for dataset, stem in [
            ("floreser", "floreser_2025"),
            ("simex", "simex_unificado_2025"),
            ("ameaca_pressao", "ameaca_e_pressao_1_trimestre_2025"),
        ]:
            for operation in ("download", "dashboard"):
                with self.subTest(dataset=dataset, operation=operation):
                    category = dataset == "ameaca_pressao" and operation == "dashboard"
                    name = "ameaca_e_pressao_2025_geral_ameaca" if category else stem
                    with capture_uploads() as captured:
                        result = self.finish(
                            self.post(
                                dataset=dataset,
                                operation=operation,
                                files=self.sad_zip(name=name + ".geojson"),
                            )
                        )
                    self.assertEqual(result["status"], "done", result["logs"])
                    root = (
                        "ameaca_e_pressao" if dataset == "ameaca_pressao" else dataset
                    )
                    prefix = f"dashboard/{root}" if operation == "dashboard" else root
                    self.assertEqual(
                        set(captured),
                        {
                            f"{prefix}/geojson/{name}.geojson",
                            f"{prefix}/csv/{name}.csv",
                            f"{prefix}/shapefile/{name}.zip",
                        },
                    )
                    self.assertEqual(
                        len(
                            json.loads(captured[f"{prefix}/geojson/{name}.geojson"])[
                                "features"
                            ]
                        ),
                        1,
                    )
                    self.assertIn(b"Belem", captured[f"{prefix}/csv/{name}.csv"])
                    with zipfile.ZipFile(
                        io.BytesIO(captured[f"{prefix}/shapefile/{name}.zip"])
                    ) as archive:
                        self.assertTrue(
                            {".shp", ".shx", ".dbf", ".prj"}.issubset(
                                {Path(n).suffix for n in archive.namelist()}
                            )
                        )

    def test_other_layers_require_zip_with_geojson(self):
        for dataset in ("floreser", "simex", "ameaca_pressao"):
            with self.subTest(dataset=dataset):
                self.assertEqual(
                    self.post(
                        dataset=dataset, files=(io.BytesIO(b"{}"), "file.geojson")
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    self.post(
                        dataset=dataset, files=self.sad_zip(name="file.csv")
                    ).status_code,
                    400,
                )

    def test_real_requires_password(self):
        with patch.dict(os.environ, {"UPLOAD_PASSWORD": "test-password"}):
            self.assertEqual(self.post(mode="real").status_code, 400)

    def test_csrf(self):
        self.assertEqual(
            self.client.post("/api/jobs", base_url="http://localhost:5000").status_code,
            403,
        )

    def test_zip_traversal_rejected(self):
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as archive:
            archive.writestr("../escape.shp", "invalid")
        data.seek(0)
        self.assertEqual(
            self.post(dataset="sad", files=(data, "sad.zip")).status_code, 400
        )

    def test_busy(self):
        server.lock.acquire()
        try:
            self.assertEqual(self.post().status_code, 409)
        finally:
            server.lock.release()


if __name__ == "__main__":
    unittest.main()
