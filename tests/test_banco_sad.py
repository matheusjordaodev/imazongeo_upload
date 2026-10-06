"""SAD no banco: normalização, leitura em blocos e gravação por mês e parte.

Os testes com banco rodam só com BANCO_TEST_DATABASE_URL (ver
test_banco_integracao.py) e APAGAM o esquema imazongeo desse banco.
"""

from __future__ import annotations

import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from shapely.geometry import box

from banco_fixtures import geojson_sad
from imazongeo_upload.banco import carga_sad, db
from imazongeo_upload.banco import normalizacao as norm
from imazongeo_upload.s3 import usar_cliente_s3
from imazongeo_upload.sad import processar_sad_zip
from imazongeo_upload.simulador import SimuladorS3

URL = os.getenv("BANCO_TEST_DATABASE_URL")


class TestNormalizacaoSad(unittest.TestCase):
    def test_export_atual(self):
        linha = norm.linha_sad(
            {
                "ALERTA": "desmatamento",
                "MES": "5",
                "ANO": "2026",
                "SENSOR": "Sentinel-2",
                "ESTADO": "PA",
                "AREAKM2": 12.4361,
                "NM_MUN": "Altamira",
            },
            "desmatamento",
            "municipios",
        )
        self.assertEqual(
            linha,
            {
                "ano": 2026,
                "mes": 5,
                "tipo": "desmatamento",
                "camada": "municipios",
                "sensor": "Sentinel-2",
                "uf": "PA",
                "municipio": "Altamira",
                "territorio": None,
                "uso": None,
                "jurisdicao": None,
                "area_km2": 12.4361,
            },
        )
        self.assertEqual(norm.linha_sad(linha, "desmatamento", "municipios"), linha)

    def test_zip_mensal_antigo(self):
        linha = norm.linha_sad(
            {
                "Class_Name": "desmatamento",
                "Mes_Ano": "01_2026",
                "Sensor": "Landsat-8",
                "Estado": "PA",
                "Area": 2.216,
                "MUNICIPIOS": "São Félix do Xingu",
                "UC": "APA Triunfo do Xingu",
                "USO": "Uso Sustentável",
                "JURISDICAO": "Estadual",
            },
            "desmatamento",
            "unidades_conservacao",
        )
        self.assertEqual(
            (linha["ano"], linha["mes"], linha["territorio"], linha["jurisdicao"]),
            (2026, 1, "APA Triunfo do Xingu", "Estadual"),
        )

    def test_erros(self):
        base = {"ANO": "2026", "MES": "5", "ALERTA": "degradacao"}
        with self.assertRaisesRegex(ValueError, "num arquivo de desmatamento"):
            norm.linha_sad(base, "desmatamento", "municipios")
        with self.assertRaisesRegex(ValueError, "mês inválido"):
            norm.linha_sad({**base, "MES": "13"}, "degradacao", "municipios")
        with self.assertRaisesRegex(ValueError, "ano"):
            norm.linha_sad({"MES": "5"}, "degradacao", "municipios")

    def test_identificacao_pelo_nome(self):
        self.assertEqual(carga_sad.intervalo((2025, 11), (2026, 2))[-1], (2026, 2))
        self.assertEqual(len(carga_sad.intervalo((2008, 1), (2026, 7))), 223)
        arq = carga_sad.identificar(
            Path("alertas_sad_degradacao_01_2008_07_2026_terrasIndigenas.shp")
        )
        self.assertEqual(
            (arq.tipo, arq.camada, arq.particao, len(arq.meses)),
            ("degradacao", "terras_indigenas", "degradacao/terras_indigenas", 223),
        )
        with self.assertRaisesRegex(ValueError, "fora do padrão"):
            carga_sad.identificar(Path("livre.geojson"))

    def test_identificacao_de_tabela(self):
        # O Postgres corta nomes em 63 caracteres: a camada chega truncada
        for tabela, camada in [
            ("alertas_sad_desmatamento_01_2008_07_2026_municipios", "municipios"),
            (
                "alertas_sad_desmatamento_01_2008_07_2026_unidadeconserv",
                "unidades_conservacao",
            ),
            (
                "alertas_sad_degradacao_09_2008_07_2026_unidadesconserva",
                "unidades_conservacao",
            ),
            (
                "alertas_sad_desmatamento_01_2008_07_2026_terrasindigena",
                "terras_indigenas",
            ),
            ("alertas_sad_degradacao_09_2008_07_2026_amazonialegal", "amazonia_legal"),
        ]:
            t = carga_sad.identificar_tabela(tabela)
            self.assertEqual(
                (t.camada, t.esquema, t.nome),
                (camada, "imazongeo", f"imazongeo.{tabela}"),
            )
        t = carga_sad.identificar_tabela(
            "alertas_sad_degradacao_09_2008_07_2026_municipios"
        )
        self.assertEqual(
            (t.tipo, t.origem, len(t.meses)), ("degradacao", "tabela", 215)
        )
        with self.assertRaisesRegex(ValueError, "fora do padrão"):
            carga_sad.identificar_tabela("alertas_sad_desmatamento_01_2008_rios")


class TestPreviaSad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_previa_le_em_blocos_e_corrige(self):
        alertas = [
            {"ano": 2025, "mes": 1},
            {"ano": 2025, "mes": 1, "area": None},  # área vazia: recalculada
            {"ano": 2025, "mes": 2, "uso": "Uso Sustent�vel"},
            {"ano": 2025, "mes": 2, "territorio": "APA  Triunfo  do Xingu"},
        ]
        caminho = geojson_sad(self.dir, alertas)
        with (
            patch.object(carga_sad, "LOTE", 2),  # força vários blocos
            patch.object(carga_sad.db, "conectar", side_effect=AssertionError),
        ):
            r = carga_sad.gravar_caminhos([caminho])
        particao = "desmatamento/unidades_conservacao"
        self.assertEqual(r.registros[particao], 4)
        self.assertEqual(r.meses[particao], 3)  # 01 a 03/2025 (03 sem alertas)
        avisos = "\n".join(r.avisos)
        self.assertIn("1 área(s) vazia(s) ou diferente(s)", avisos)
        self.assertIn("valor(es) com grafia padronizada", avisos)
        self.assertIn("texto(s) com espaços corrigidos", avisos)

    def test_repetidos_no_mesmo_envio(self):
        geom = box(-52, -5, -51.99, -4.99)
        alertas = [{"ano": 2025, "mes": 1, "geom": geom}] * 3
        r = carga_sad.gravar_caminhos([geojson_sad(self.dir, alertas)])
        self.assertEqual(sum(r.registros.values()), 1)
        self.assertIn("2 registro(s) repetido(s) removido(s)", "\n".join(r.avisos))

    def test_zip_com_duas_partes_e_arquivo_repetido(self):
        a = geojson_sad(self.dir, [{"ano": 2025, "mes": 1}])
        b = geojson_sad(self.dir, [{"ano": 2025, "mes": 1}], camada="municipios")
        caminho = self.dir / "sad.zip"
        with zipfile.ZipFile(caminho, "w") as zf:
            zf.write(a, f"shapefile/{a.name}")
            zf.write(b, b.name)
        r = carga_sad.gravar_caminhos([caminho])
        self.assertEqual(
            set(r.registros),
            {"desmatamento/unidades_conservacao", "desmatamento/municipios"},
        )
        with self.assertRaisesRegex(ValueError, "Mais de uma fonte"):
            carga_sad.gravar(
                [carga_sad.identificar(a), carga_sad.identificar(a)], "dry_run"
            )

    def test_modo_banco(self):
        with patch.dict(os.environ, {"DATABASE_URL": ""}):
            self.assertIsNone(carga_sad.modo_banco(dry_run=False))
        with patch.dict(os.environ, {"DATABASE_URL": "postgresql:///x"}):
            self.assertIsNone(carga_sad.modo_banco(dry_run=True))
            self.assertEqual(carga_sad.modo_banco(False), "real")
            self.assertEqual(carga_sad.modo_banco(False, simulacao=True), "simulation")


@unittest.skipUnless(
    URL, "defina BANCO_TEST_DATABASE_URL (banco descartável com PostGIS)"
)
class TestBancoSad(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        with db.conectar(URL) as conn:
            with conn, conn.cursor() as cur:
                cur.execute("DROP SCHEMA IF EXISTS imazongeo CASCADE")
            db.criar_schema(conn, ["sad"])

    def tearDown(self):
        self.tmp.cleanup()

    def consulta(self, sql: str, *valores) -> list[tuple]:
        with db.conectar(URL) as conn, conn, conn.cursor() as cur:
            cur.execute(sql, valores)
            return cur.fetchall()

    def gravar(self, *caminhos: Path, modo: str = "real"):
        return carga_sad.gravar_caminhos(list(caminhos), modo, URL)

    def test_so_o_sad_e_criado(self):
        tabelas = self.consulta(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'imazongeo' ORDER BY 1"
        )
        self.assertEqual(
            tabelas,
            [("carga",), ("dataset",), ("publicacao",), ("sad_alerta",), ("vw_sad",)],
        )
        self.assertEqual(
            self.consulta("SELECT slug FROM imazongeo.dataset"), [("sad",)]
        )

    def test_grava_por_mes_e_visao_do_dashboard(self):
        self.gravar(
            geojson_sad(
                self.dir,
                [
                    {"ano": 2025, "mes": 1},
                    {"ano": 2025, "mes": 1},
                    {"ano": 2025, "mes": 2},
                ],
            )
        )
        self.assertEqual(
            self.consulta(
                "SELECT ano, mes, particao, registros FROM imazongeo.carga "
                "WHERE vigente ORDER BY mes"
            ),
            [
                (2025, 1, "desmatamento/unidades_conservacao", 2),
                (2025, 2, "desmatamento/unidades_conservacao", 1),
                (2025, 3, "desmatamento/unidades_conservacao", 0),
            ],
        )
        # A mesma consulta do dashboard do SAD (banco.js)
        linhas = self.consulta(
            'SELECT tipo AS "ALERTA", mes AS "MES", ano AS "ANO", sensor AS "SENSOR",'
            ' uf AS "ESTADO", municipio AS "MUNICIPIO", territorio AS "UNID_CONSE",'
            ' uso AS "USO", jurisdicao AS "JURISDICAO",'
            ' round(sum(area_km2)::numeric, 4) AS "AREAKM2" FROM imazongeo.vw_sad'
            " WHERE tipo = %s AND camada = %s"
            " GROUP BY tipo, mes, ano, sensor, uf, municipio, territorio, uso,"
            " jurisdicao ORDER BY ano, mes",
            "desmatamento",
            "unidades_conservacao",
        )
        self.assertEqual(len(linhas), 2)
        self.assertEqual(linhas[0][:5], ("desmatamento", 1, 2025, "Sentinel-2", "PA"))
        self.assertEqual(float(linhas[0][-1]), 2.4616)

    def test_reenvio_substitui_so_a_parte_e_os_meses(self):
        ucs = [{"ano": 2025, "mes": m} for m in (1, 2, 3)]
        self.gravar(geojson_sad(self.dir, ucs))
        self.gravar(
            geojson_sad(self.dir, [{"ano": 2025, "mes": 1}], camada="municipios")
        )
        # Novo envio de UC só de 02 a 03/2025, com um alerta a mais em 02
        novos = [{"ano": 2025, "mes": 2}, {"ano": 2025, "mes": 2}]
        self.gravar(geojson_sad(self.dir, novos, inicio=(2, 2025), fim=(3, 2025)))
        self.assertEqual(
            self.consulta(
                "SELECT camada, mes, count(*) FROM imazongeo.vw_sad "
                "GROUP BY 1, 2 ORDER BY 1, 2"
            ),
            [
                ("municipios", 1, 1),  # outra parte: intacta
                ("unidades_conservacao", 1, 1),  # fora do intervalo: intacto
                ("unidades_conservacao", 2, 2),  # substituído
            ],  # 03/2025 ficou sem alertas
        )
        self.assertEqual(
            self.consulta(
                "SELECT count(*) FILTER (WHERE vigente), count(*) FROM imazongeo.carga"
            ),
            [(6, 8)],  # 3 (UC) + 3 (municípios) + 2 novas; 2 substituídas
        )

    def test_simulacao_nao_altera_o_banco(self):
        r = self.gravar(
            geojson_sad(self.dir, [{"ano": 2025, "mes": 1}]), modo="simulation"
        )
        self.assertEqual(sum(r.registros.values()), 1)
        self.assertEqual(self.consulta("SELECT count(*) FROM imazongeo.carga"), [(0,)])

    def test_envio_do_app_grava_no_banco_antes_do_s3(self):
        arquivo = geojson_sad(self.dir, [{"ano": 2025, "mes": 3}])
        caminho = self.dir / "sad.zip"
        with zipfile.ZipFile(caminho, "w") as zf:
            zf.write(arquivo, arquivo.name)
        s3 = SimuladorS3(self.dir / "_s3")
        with (
            usar_cliente_s3(s3),
            patch.object(carga_sad.db, "database_url", return_value=URL),
        ):
            processar_sad_zip([caminho], "bucket", False, banco="real")
        self.assertEqual(self.consulta("SELECT count(*) FROM imazongeo.vw_sad"), [(1,)])
        self.assertTrue(s3.uploads)
        # Falha no banco: nada vai para o S3
        s3 = SimuladorS3(self.dir / "_s3_2")
        with (
            usar_cliente_s3(s3),
            patch.object(carga_sad, "gravar", side_effect=RuntimeError("banco")),
            self.assertRaisesRegex(RuntimeError, "banco"),
        ):
            processar_sad_zip([caminho], "bucket", False, banco="real")
        self.assertEqual(s3.uploads, [])


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(
    URL, "defina BANCO_TEST_DATABASE_URL (banco descartável com PostGIS)"
)
class TestTabelasExistentes(unittest.TestCase):
    """Banco como o da VM: tabelas importadas com ogr2ogr e uma vw_sad própria."""

    TABELA = "alertas_sad_desmatamento_01_2025_03_2025_unidadeconserv"

    def setUp(self):
        with db.conectar(URL) as conn, conn, conn.cursor() as cur:
            cur.execute("DROP SCHEMA IF EXISTS imazongeo CASCADE")
            cur.execute("CREATE SCHEMA imazongeo")
            cur.execute(f"""
                CREATE TABLE imazongeo.{self.TABELA} (
                    ogc_fid serial PRIMARY KEY, geom geometry(MultiPolygon, 4326),
                    alerta varchar, mes varchar, ano varchar, sensor varchar,
                    estado varchar, areakm2 float8, municipios varchar, uc varchar,
                    uso varchar, jurisdicao varchar)""")
            cur.execute(
                f"INSERT INTO imazongeo.{self.TABELA} (geom, alerta, mes, ano,"
                " sensor, estado, areakm2, municipios, uc, uso, jurisdicao) VALUES"
                " (ST_Multi(ST_MakeEnvelope(-52, -5, -51.99, -4.99, 4326)),"
                "  'desmatamento', '1', '2025', 'Sentinel-2', 'PA', 1.2308,"
                "  'Altamira', 'APA Triunfo do Xingu', 'uso sustentavel', 'Estadual'),"
                " (ST_Multi(ST_MakeEnvelope(-52.1, -5, -52.09, -4.99, 4326)),"
                "  'desmatamento', '2', '2025', 'Sentinel-2', 'PA', 1.2308,"
                "  'Altamira', 'APA  Triunfo do Xingu', 'Uso Sustentável', 'Estadual')"
            )
            # vw_sad anterior, direto da tabela crua (tipos diferentes dos nossos)
            cur.execute(
                f"CREATE VIEW imazongeo.vw_sad AS SELECT 'desmatamento'::text AS tipo,"
                " 'unidades_conservacao'::text AS camada, mes::integer AS mes,"
                " ano::integer AS ano, sensor, estado AS uf, municipios AS municipio,"
                " uc AS territorio, uso, jurisdicao, areakm2::numeric AS area_km2, geom"
                f" FROM imazongeo.{self.TABELA}"
            )

    def consulta(self, sql: str) -> list[tuple]:
        with db.conectar(URL) as conn, conn, conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall()

    def test_migra_tabela_para_o_modelo_e_troca_a_visao(self):
        self.assertEqual(self.consulta("SELECT count(*) FROM imazongeo.vw_sad"), [(2,)])
        r = carga_sad.gravar_tabelas(None, "real", URL)
        self.assertEqual(sum(r.registros.values()), 2)

        # Os alertas agora estão no modelo, com as correções do padrão
        self.assertEqual(
            self.consulta(
                "SELECT tipo, camada, ano, mes, uf, territorio, uso FROM"
                " imazongeo.sad_alerta ORDER BY mes"
            ),
            [
                (
                    "desmatamento",
                    "unidades_conservacao",
                    2025,
                    1,
                    "PA",
                    "APA Triunfo do Xingu",
                    "Uso Sustentável",
                ),
                (
                    "desmatamento",
                    "unidades_conservacao",
                    2025,
                    2,
                    "PA",
                    "APA Triunfo do Xingu",
                    "Uso Sustentável",
                ),
            ],
        )
        # Uma carga por mês do intervalo do nome, com origem 'tabela'
        self.assertEqual(
            self.consulta(
                "SELECT ano, mes, origem, registros, arquivo FROM imazongeo.carga"
                " WHERE vigente ORDER BY mes"
            ),
            [
                (2025, 1, "tabela", 1, f"imazongeo.{self.TABELA}"),
                (2025, 2, "tabela", 1, f"imazongeo.{self.TABELA}"),
                (2025, 3, "tabela", 0, f"imazongeo.{self.TABELA}"),
            ],
        )
        # A visão passou a vir de sad_alerta e a tabela de origem ficou intacta
        self.assertIn(
            "sad_alerta",
            self.consulta("SELECT pg_get_viewdef('imazongeo.vw_sad'::regclass)")[0][0],
        )
        self.assertEqual(self.consulta("SELECT count(*) FROM imazongeo.vw_sad"), [(2,)])
        self.assertEqual(
            self.consulta(f"SELECT count(*) FROM imazongeo.{self.TABELA}"), [(2,)]
        )

    def test_previa_nao_altera_nada(self):
        r = carga_sad.gravar_tabelas(None, "dry_run", URL)
        self.assertEqual(sum(r.registros.values()), 2)
        self.assertEqual(
            self.consulta(
                "SELECT count(*) FROM information_schema.tables"
                " WHERE table_schema = 'imazongeo' AND table_name = 'sad_alerta'"
            ),
            [(0,)],
        )
