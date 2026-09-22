"""Testes do padrão, da validação de envios e da exportação (sem banco)."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import geopandas as gpd
import numpy as np
import pandas as pd
import pyogrio
import shapely
from shapely.geometry import MultiPolygon, Polygon, box

from banco_fixtures import csv_floreser, geojson_ap, zip_simex
from imazongeo_upload.banco import correcao, entrada, espelho, exportacao, fluxo
from imazongeo_upload.banco import normalizacao as norm
from imazongeo_upload.banco.padrao import CAMADAS_SIMEX, PADROES, Periodo, padrao


class TestPadrao(unittest.TestCase):
    def test_nomes_de_campo_cabem_no_shapefile(self):
        for p in PADROES.values():
            for campo in p.campos:
                self.assertLessEqual(len(campo.nome), 10, campo.nome)

    def test_chaves_s3(self):
        self.assertEqual(
            padrao("simex").chave_s3(Periodo(2024), "shapefile"),
            "simex/shapefile/simex_2024.zip",
        )
        self.assertEqual(
            padrao("ameaca_pressao").chave_s3(Periodo(2025, 3), "geojson"),
            "ameaca_e_pressao/geojson/ameaca_e_pressao_2025_t3.geojson",
        )
        self.assertEqual(padrao("floreser").formatos, ("csv",))

    def test_periodo(self):
        self.assertEqual(Periodo(2025, mes=9).sufixo, "2025_09")
        with self.assertRaises(ValueError):
            Periodo(2025, trimestre=5)
        with self.assertRaises(ValueError):
            padrao("ameaca_pressao").validar_periodo(Periodo(2025))
        with self.assertRaises(ValueError):
            padrao("simex").validar_periodo(Periodo(2025, 1))


class TestNormalizacao(unittest.TestCase):
    # Propriedades como nos GeoJSON até 2023
    SIMEX_2023_UC = {
        "sigla_uf": "MT",
        "area_ha": 36.26,
        "categoria": "não autorizada",
        "ano": "2023",
        "name": None,
        "nome_1": "PARQUE NACIONAL DO JURUENA",
        "sub_class": None,
        "terrai_nom": None,
        "nome": "Nova Bandeirantes",
        "geocodigo": "5106158",
        "source_layer": "UC",
    }
    # Propriedades como no shapefile de 2024
    SIMEX_2024_UC = {
        "UF": "RO",
        "area_ha": 76.45,
        "categoria": "não autorizada",
        "Ano": "2024",
        "NM_MUN": "Candeias do Jamari",
        "nome": "ESTAÇÃO ECOLÓGICA SAMUEL",
        "cd_mun": None,
        "geocodigo": None,
        "fonte": "unidades_conservacao",
    }

    def test_simex_ate_2023(self):
        self.assertEqual(
            norm.linha_simex(self.SIMEX_2023_UC),
            {
                "ano": 2023,
                "camada": "unidades_conservacao",
                "categoria": "não autorizada",
                "uf": "MT",
                "municipio": "Nova Bandeirantes",
                "cod_mun": 5106158,
                "territorio": "PARQUE NACIONAL DO JURUENA",
                "subclasse": None,
                "area_ha": 36.26,
            },
        )

    def test_simex_2024_e_padrao_novo(self):
        linha = norm.linha_simex(self.SIMEX_2024_UC)
        self.assertEqual(
            (linha["uf"], linha["municipio"], linha["territorio"]),
            ("RO", "Candeias do Jamari", "ESTAÇÃO ECOLÓGICA SAMUEL"),
        )
        # Um arquivo publicado no padrão novo, reenviado, dá o mesmo registro
        self.assertEqual(norm.linha_simex(linha), linha)

    def test_simex_categorias_e_camadas(self):
        base = dict(self.SIMEX_2023_UC, source_layer="mun")
        self.assertEqual(norm.linha_simex(base)["camada"], "municipios")
        self.assertIsNone(norm.linha_simex(base)["territorio"])
        analise = dict(base, categoria="análise")
        self.assertEqual(norm.linha_simex(analise)["categoria"], "análise")
        self.assertIsNone(norm.linha_simex(dict(base, categoria=None))["categoria"])
        with self.assertRaises(ValueError):
            norm.linha_simex(dict(base, categoria="talvez"))
        with self.assertRaises(ValueError):
            norm.linha_simex(dict(base, source_layer="rios"))
        # Sem atributo, a camada vem do nome do arquivo
        sem = dict(base, source_layer=None)
        self.assertEqual(
            norm.linha_simex(sem, "terras_indigenas")["camada"], "terras_indigenas"
        )

    def test_camada_pelo_nome_do_arquivo(self):
        for nome, camada in [
            ("simex_amz_2024_imoveisruraisprivados(1).geojson", "imoveis_rurais"),
            ("simex_amz_2024_TI(2).geojson", "terras_indigenas"),
            ("simex_amz_PAMTM_TerrasNDest.geojson", "terras_nao_destinadas"),
            ("simex_amz_terras_indigenas.shp", "terras_indigenas"),
            ("simex_amz_2024_municipios.geojson", "municipios"),
            ("simex_2024.geojson", None),
        ]:
            self.assertEqual(norm.camada_simex(nome, exato=False), camada, nome)

    def test_completar_cod_mun(self):
        linhas = [
            {"uf": "RO", "municipio": "Porto Velho", "cod_mun": 1100205},
            {"uf": "RO", "municipio": "Porto Velho", "cod_mun": None},
            {"uf": "AM", "municipio": "Porto Velho", "cod_mun": None},
        ]
        norm.completar_cod_mun(linhas)
        self.assertEqual([x["cod_mun"] for x in linhas], [1100205, 1100205, None])

    def test_ap(self):
        linha = norm.linha_ap(
            {
                "dado": "geral",
                "class": "pressao",
                "nome": "RESEX Chico Mendes",
                "modalidade": "UCF",
                "rank": np.int32(1),
                "celulas": np.int64(98),
                "ano": 2022,
                "periodo": "2",
                "legend": "abril a junho",
            }
        )
        self.assertEqual(
            (
                linha["modalidade"],
                linha["posicao"],
                linha["celulas"],
                linha["trimestre"],
            ),
            ("ucf", 1, 98, 2),
        )
        self.assertEqual(norm.linha_ap(linha), linha)  # padrão novo
        self.assertEqual(
            norm.linha_ap({**linha, "trimestre": None, "mes": 8})["trimestre"], 3
        )
        self.assertEqual(
            norm.grupo_ap("ameaca_e_pressao_2025_categ_ti_ameaca.geojson"),
            ("categ_ti", "ameaca"),
        )
        with self.assertRaises(ValueError):
            norm.linha_ap({**linha, "classe": "outra"})

    def test_floreser(self):
        legado = {
            "index": "38_632",
            "ano": "2024",
            "area": "4002.48",
            "CD_GEOCUF": "11",
            "cod_municipio": "1100205",
            "IDADE": "11",
            "estado": "Rondônia",
            "nome": "Porto Velho",
        }
        linha = norm.linha_floreser(legado)
        self.assertEqual(
            linha,
            {
                "ano": 2024,
                "cod_uf": 11,
                "estado": "Rondônia",
                "cod_mun": 1100205,
                "municipio": "Porto Velho",
                "idade": 11,
                "area_ha": 4002.48,
            },
        )
        self.assertEqual(norm.linha_floreser(linha), linha)
        with self.assertRaises(ValueError):
            norm.linha_floreser({**legado, "IDADE": ""})

    def test_propriedades(self):
        gdf = gpd.GeoDataFrame(
            {"a": [np.int32(1), None], "b": [1.5, np.nan]},
            geometry=[box(0, 0, 1, 1)] * 2,
        )
        self.assertEqual(
            norm.propriedades(gdf), [{"a": 1, "b": 1.5}, {"a": None, "b": None}]
        )


class TestEntrada(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_simex_zip_por_camada(self):
        envio = entrada.preparar_envio(zip_simex(self.dir), "simex", Periodo(2025))
        self.assertEqual(envio.registros, 6)
        self.assertEqual({x["camada"] for x in envio.linhas}, set(CAMADAS_SIMEX))
        ti = next(x for x in envio.linhas if x["camada"] == "terras_indigenas")
        self.assertEqual(ti["cod_mun"], 1505502)  # completado pelo município
        self.assertEqual(ti["territorio"], "Alto Rio Guamá")
        g = shapely.from_wkb(envio.wkbs[0])
        self.assertEqual((g.geom_type, g.has_z), ("MultiPolygon", False))

    def test_simex_exige_todas_as_camadas_e_o_ano(self):
        with self.assertRaisesRegex(ValueError, "unidades_conservacao"):
            entrada.preparar_envio(
                zip_simex(self.dir, sem="unidades_conservacao"), "simex", Periodo(2025)
            )
        with self.assertRaisesRegex(ValueError, "outro"):
            entrada.preparar_envio(
                zip_simex(self.dir, ano=2024), "simex", Periodo(2025)
            )

    def test_shapefile_no_zip(self):
        gdf = gpd.GeoDataFrame(
            {"camada": ["municipios"], "area_ha": [1.0], "ano": [2025]},
            geometry=[box(0, 0, 1, 1)],
            crs=4326,
        )
        pasta = self.dir / "shp"
        pasta.mkdir()
        gdf.to_file(pasta / "municipios.shp")
        caminho = self.dir / "shp.zip"
        with zipfile.ZipFile(caminho, "w") as zf:
            for parte in pasta.iterdir():
                zf.write(parte, f"pasta/{parte.name}")
        camadas = entrada.ler_camadas(caminho, self.dir / "x")
        self.assertEqual([nome for nome, _ in camadas], ["municipios.shp"])

    def test_zip_invalido(self):
        mistura = self.dir / "mistura.zip"
        with zipfile.ZipFile(mistura, "w") as zf:
            zf.writestr("a.geojson", "{}")
            zf.writestr("b.shp", "")
        with self.assertRaisesRegex(ValueError, "único formato"):
            entrada.ler_camadas(mistura, self.dir / "y")
        fuga = self.dir / "fuga.zip"
        with zipfile.ZipFile(fuga, "w") as zf:
            zf.writestr("../fora.geojson", "{}")
        with self.assertRaisesRegex(ValueError, "caminho inválido"):
            entrada.ler_camadas(fuga, self.dir / "z")
        with self.assertRaisesRegex(ValueError, ".zip"):
            entrada.ler_camadas(self.dir / "dados.csv", self.dir)

    def test_ap_filtra_o_trimestre_e_exige_os_rankings(self):
        t2 = gpd.read_file(geojson_ap(self.dir, trimestre=2))
        t3 = gpd.read_file(geojson_ap(self.dir, trimestre=3))
        anual = self.dir / "anual.geojson"
        pd.concat([t2, t3]).to_file(anual, driver="GeoJSON")
        envio = entrada.preparar_envio(anual, "ameaca_pressao", Periodo(2025, 3))
        self.assertEqual(envio.registros, 8)
        self.assertEqual({x["trimestre"] for x in envio.linhas}, {3})
        self.assertTrue(envio.avisos)
        incompleto = self.dir / "incompleto.geojson"
        t3[t3["dado"] != "geral"].to_file(incompleto, driver="GeoJSON")
        with self.assertRaisesRegex(ValueError, "geral/ameaca"):
            entrada.preparar_envio(incompleto, "ameaca_pressao", Periodo(2025, 3))

    def test_floreser_csv(self):
        envio = entrada.preparar_envio(
            csv_floreser(self.dir), "floreser", Periodo(2024)
        )
        self.assertEqual(envio.registros, 2)
        self.assertIsNone(envio.wkbs)
        texto = csv_floreser(self.dir).read_text(encoding="utf-8")
        linha = texto.splitlines()[1]
        # Linha idêntica repetida: removida
        repetido = self.dir / "rep.csv"
        repetido.write_text(texto + linha + "\n", encoding="utf-8")
        envio = entrada.preparar_envio(repetido, "floreser", Periodo(2024))
        self.assertEqual(envio.registros, 2)
        self.assertIn("2024: 1 registro(s) repetido(s) removido(s)", envio.avisos)
        # Mesma UF/município/idade com área diferente: não há como escolher
        conflito = self.dir / "conflito.csv"
        conflito.write_text(texto + linha.replace("10.5", "99") + "\n", "utf-8")
        with self.assertRaisesRegex(ValueError, "áreas diferentes"):
            entrada.preparar_envio(conflito, "floreser", Periodo(2024))
        with self.assertRaisesRegex(ValueError, ".zip"):
            entrada.preparar_envio(csv_floreser(self.dir), "simex", Periodo(2024))

    def test_geometrias(self):
        poligono_3d = Polygon([(-50, -3, 0), (-49, -3, 0), (-49, -2, 0), (-50, -3, 0)])
        gdf = gpd.GeoDataFrame(
            geometry=[poligono_3d, MultiPolygon([box(-51, -4, -50, -3)])], crs=4674
        )
        geoms = [shapely.from_wkb(w) for w in entrada.geometrias_wkb(gdf)]
        self.assertTrue(
            all(g.geom_type == "MultiPolygon" and not g.has_z for g in geoms)
        )
        with self.assertRaises(ValueError):
            entrada.geometrias_wkb(gpd.GeoDataFrame(geometry=[Polygon()], crs=4326))
        with self.assertRaises(ValueError):
            entrada.geometrias_wkb(gpd.GeoDataFrame(geometry=[box(0, 0, 1, 1)]))

    def test_geometria_invalida_corrigida_sem_perder_area(self):
        # Anel que toca a si mesmo em (1, 1): o caso dos 175 polígonos do SIMEX
        oito = Polygon([(0, 0), (2, 0), (1, 1), (2, 2), (0, 2), (1, 1), (0, 0)])
        self.assertFalse(oito.is_valid)
        contador = correcao.Contador()
        gdf = gpd.GeoDataFrame(geometry=[oito, box(5, 5, 6, 6)], crs=4326)
        geoms = [
            shapely.from_wkb(w) for w in entrada.geometrias_wkb(gdf, "ti", contador)
        ]
        self.assertTrue(all(g.is_valid for g in geoms))
        self.assertEqual(len(geoms[0].geoms), 2)
        self.assertAlmostEqual(geoms[0].area, oito.area)
        self.assertEqual(
            contador.avisos("ti"), ["ti: 1 geometria(s) inválida(s) corrigida(s)"]
        )
        # Polígono com um "espinho" (linha sem área): a linha é descartada
        espinho = Polygon([(0, 0), (2, 0), (2, 2), (0, 2), (0, 1), (-1, 1), (0, 1)])
        self.assertFalse(espinho.is_valid)
        corrigido = entrada.reparar(espinho, contador)
        self.assertTrue(corrigido.is_valid)
        self.assertAlmostEqual(corrigido.area, 4.0)
        # Polígono sem área nenhuma (só linha): erro
        reta = Polygon([(0, 0), (1, 1), (2, 2), (0, 0)])
        with self.assertRaisesRegex(ValueError, "ti, feição 1: .*sem área"):
            entrada.geometrias_wkb(gpd.GeoDataFrame(geometry=[reta], crs=4326), "ti")

    def test_simex_corrige_texto_area_e_repetidos(self):
        envio = entrada.preparar_envio(
            zip_simex(
                self.dir, gleba="GLEBA BRAÃ\u0083â\u0080¡O  FORTE", repetido=True
            ),
            "simex",
            Periodo(2025),
        )
        self.assertEqual(envio.registros, 6)  # o assentamento repetido saiu
        gleba = next(x for x in envio.linhas if x["camada"] == "assentamentos")
        self.assertEqual(gleba["territorio"], "GLEBA BRAÇO FORTE")
        # area_ha do arquivo (10 + i) é trocada pela área do polígono
        for linha, w in zip(envio.linhas, envio.wkbs, strict=True):
            self.assertAlmostEqual(
                linha["area_ha"], correcao.area_ha(shapely.from_wkb(w)), places=4
            )
        self.assertGreater(envio.linhas[0]["area_ha"], 1000)
        avisos = "\n".join(envio.avisos)
        self.assertIn("1 registro(s) repetido(s) removido(s)", avisos)
        self.assertIn("texto(s) com codificação corrigida", avisos)
        self.assertIn("área(s) recalculada(s) pela geometria", avisos)
        # Os atributos originais ficam como vieram
        self.assertIn("Ã", str(envio.atributos))

    def test_ap_padroniza_grafias_e_remove_repetidos(self):
        t3 = gpd.read_file(geojson_ap(self.dir, legenda="janeiro a marco"))
        dobrado = self.dir / "dobrado.geojson"
        pd.concat([t3, t3]).to_file(dobrado, driver="GeoJSON")
        envio = entrada.preparar_envio(dobrado, "ameaca_pressao", Periodo(2025, 3))
        self.assertEqual(envio.registros, 8)
        self.assertEqual({x["uso"] for x in envio.linhas}, {"Terra Indígena"})
        self.assertEqual({x["legenda"] for x in envio.linhas}, {"janeiro a março"})
        self.assertIn("2025_t3: 8 registro(s) repetido(s) removido(s)", envio.avisos)
        # A mesma área no mesmo ranking com dados diferentes continua erro
        conflito = self.dir / "conflito.geojson"
        pd.concat([t3, t3.iloc[[0]].assign(celulas=1)]).to_file(
            conflito, driver="GeoJSON"
        )
        with self.assertRaisesRegex(ValueError, "dados diferentes"):
            entrada.preparar_envio(conflito, "ameaca_pressao", Periodo(2025, 3))


class TestCorrecao(unittest.TestCase):
    def test_codificacao(self):
        for quebrado, certo in [
            ("CARAJÃ\u0083Â\u0081S - I PARTE", "CARAJÁS - I PARTE"),
            ("GLEBA TAILÃ\u0083â\u0080\u009aNDIA II", "GLEBA TAILÂNDIA II"),
            ("PAF JEQUITIBÃƒÂ\x81", "PAF JEQUITIBÁ"),
            ("ABUNÃ\u0083Æ\u0092", "ABUNÃ"),
            ("Goian├®sia do Par├í", "Goianésia do Pará"),
            # Textos corretos não mudam
            ("FLORESTA NACIONAL DO TRAIRÃO", "FLORESTA NACIONAL DO TRAIRÃO"),
            ("São Félix do Xingu", "São Félix do Xingu"),
            ("Âmbar", "Âmbar"),
            ("Nº 12 – “teste”", "Nº 12 – “teste”"),
        ]:
            self.assertEqual(correcao.corrigir_codificacao(quebrado), certo)
        self.assertEqual(correcao.limpar_texto("  a   b "), "a b")
        self.assertIsNone(correcao.limpar_texto("   "))

    def test_vocabularios(self):
        usos = correcao.USOS_AP
        self.assertEqual(
            correcao.padronizar("Uso Sustent\ufffdvel", usos), "Uso Sustentável"
        )
        self.assertEqual(correcao.padronizar("TERRA INDIGENA", usos), "Terra Indígena")
        self.assertEqual(correcao.padronizar("Outra", usos), "Outra")
        self.assertEqual(
            correcao.padronizar_palavras("agosto/2019 a julho/2020", correcao.MESES),
            "agosto/2019 a julho/2020",
        )
        conhecidos = ["APA Arquipélago do Marajó", "APA do Lago de Tucuruí"]
        self.assertEqual(
            correcao.nome_conhecido("APA Arquip\ufffdlago do Maraj\ufffd", conhecidos),
            "APA Arquipélago do Marajó",
        )
        self.assertIsNone(correcao.nome_conhecido("APA Nova\ufffd", conhecidos))

    def test_area_geodesica(self):
        # 1 grau x 1 grau no equador ≈ 1.236.000 ha
        self.assertAlmostEqual(correcao.area_ha(box(0, 0, 1, 1)) / 1e6, 1.2308, 3)


class TestExportacao(unittest.TestCase):
    def test_tres_formatos_com_os_mesmos_campos(self):
        p = padrao("ameaca_pressao")
        with tempfile.TemporaryDirectory() as tmp:
            envio = entrada.preparar_envio(
                geojson_ap(Path(tmp)), "ameaca_pressao", Periodo(2025, 3)
            )
            df = pd.DataFrame(envio.linhas)[p.nomes]
            gdf = gpd.GeoDataFrame(df, geometry=shapely.from_wkb(envio.wkbs), crs=4326)
            arquivos = exportacao.escrever_arquivos(
                gdf, p, Periodo(2025, 3), Path(tmp) / "saida"
            )
            self.assertEqual(
                [a.chave_s3 for a in arquivos],
                [
                    p.chave_s3(Periodo(2025, 3), f)
                    for f in ("geojson", "csv", "shapefile")
                ],
            )
            geo = pyogrio.read_dataframe(arquivos[0].caminho)
            csv = pd.read_csv(arquivos[1].caminho, encoding="utf-8-sig")
            shp = pyogrio.read_dataframe(f"/vsizip/{arquivos[2].caminho}")
            for lido in (geo, csv, shp):
                self.assertEqual([c for c in lido.columns if c != "geometry"], p.nomes)
            self.assertTrue(
                arquivos[1].caminho.read_bytes().startswith(b"\xef\xbb\xbf")
            )
            # O arquivo publicado pode ser reenviado e gera os mesmos registros
            for a in (arquivos[0], arquivos[2]):
                de_novo = entrada.preparar_envio(
                    a.caminho, "ameaca_pressao", Periodo(2025, 3)
                )
                self.assertEqual(de_novo.linhas, envio.linhas)

    def test_recusa_vazio(self):
        with self.assertRaises(ValueError):
            exportacao.escrever_arquivos(
                pd.DataFrame(), padrao("floreser"), Periodo(2024), Path("x")
            )


class TestFluxoSemBanco(unittest.TestCase):
    def test_previa_nao_abre_banco_nem_s3(self):
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.object(fluxo.db, "conectar", side_effect=AssertionError("banco")),
            patch.object(fluxo, "create_s3_client", side_effect=AssertionError("S3")),
        ):
            r = fluxo.processar_envio(
                csv_floreser(Path(tmp)), "floreser", Periodo(2024), bucket="b"
            )
        self.assertEqual((r.modo, r.envio.registros), ("dry_run", 2))

    def test_simulacao_recusa_cliente_s3_real(self):
        import boto3

        real = boto3.client(
            "s3",
            region_name="us-east-1",
            aws_access_key_id="x",
            aws_secret_access_key="y",
        )
        with (
            patch.object(fluxo, "create_s3_client", return_value=real),
            self.assertRaisesRegex(RuntimeError, "Simulação"),
        ):
            fluxo._cliente_s3("simulation")


class TestEspelho(unittest.TestCase):
    def test_chaves_legadas_e_escolha(self):
        lista = espelho.candidatos(["ameaca_pressao", "floreser"], 2020, 2021)
        self.assertEqual(len([a for a in lista if a.dataset == "floreser"]), 2)
        self.assertEqual(len(lista), 2 + 2 * 4 * 3)
        self.assertEqual(
            espelho.chave_legada("ameaca_pressao", Periodo(2025, 3), "shapefile"),
            "ameaca_e_pressao/shapefile/ameaca_e_pressao_3_trimestre_2025.zip",
        )
        escolhidos = espelho.escolher_por_periodo(
            espelho.candidatos(["simex"], 2020, 2020)
        )
        self.assertEqual([a.formato for a in escolhidos], ["geojson"])

    def test_manifesto_e_etag(self):
        with tempfile.TemporaryDirectory() as tmp:
            destino = Path(tmp)
            md5_abc = "900150983cd24fb0d6963f7d28e17f72"
            a = espelho.ArquivoLegado(
                "floreser",
                Periodo(2000),
                "csv",
                "floreser/floreser_2000.csv",
                tamanho=3,
                etag=md5_abc,
                modificado_em=datetime(2025, 10, 20, tzinfo=timezone.utc),
            )
            alvo = destino / a.chave
            alvo.parent.mkdir(parents=True)
            alvo.write_bytes(b"abc")
            # Já baixado: não acessa a rede
            self.assertEqual(espelho.baixar_arquivo(a, destino, 1), (alvo, False))
            espelho.gravar_manifesto([a], destino)
            lido = espelho.ler_manifesto(destino)[0]
            self.assertEqual(
                (lido.periodo, lido.modificado_em), (a.periodo, a.modificado_em)
            )
            a.etag = "0" * 32  # mesmo tamanho, conteúdo diferente no S3
            self.assertFalse(espelho.atualizado(a, alvo))
            a.etag = "abc-4"  # multipart: compara com o último download
            self.assertTrue(espelho.atualizado(a, alvo, "abc-4"))
            self.assertFalse(espelho.atualizado(a, alvo, "def-4"))


if __name__ == "__main__":
    unittest.main()
