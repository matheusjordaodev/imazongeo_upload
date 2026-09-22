"""SAD: atualização a partir do ZIP recebido com os alertas acumulados.

O ZIP traz um shapefile (ou GeoJSON) por tipo de alerta + camada. A partir
dele são gerados os CSVs do dashboard e os arquivos mensais de download.
"""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .datasets import DATASETS, FORMATS, s3_dashboard_prefix
from .s3 import baixar_arquivo_s3, create_s3_client, enviar_arquivo_s3
from .utils import zipar_diretorio

if TYPE_CHECKING:
    import pandas as pd

# Nome da camada no arquivo recebido (em minúsculas) → nome padronizado
# usado nos arquivos de saída. O export do SAD não é consistente entre os
# tipos ("terrasIndigenas" x "terraIndigena", "unidadesConservacao" x
# "unidadeConservacao"), por isso as variações.
_SAD_CAMADAS = {
    "amazonialegal": "amazoniaLegal",
    "municipios": "municipios",
    "assentamentos": "assentamentos",
    "terrasindigenas": "terraIndigena",
    "terraindigena": "terraIndigena",
    "unidadesconservacao": "unidadeConservacao",
    "unidadeconservacao": "unidadeConservacao",
}

# alertas_sad_{tipo}_{MM}_{AAAA}[_{MM}_{AAAA}]_{camada}[__polygons]
_SAD_NOME_RE = re.compile(
    r"^alertas_sad_(?P<tipo>desmatamento|degradacao)_"
    r"(?P<periodos>(?:\d{1,2}_\d{4}_)+)"
    r"(?P<camada>[A-Za-z]+)(?:__polygons)?$",
    re.IGNORECASE,
)

# Nome dos CSVs em dashboard/sad/csv/ por tipo + camada, no padrão já
# consumido pelo dashboard — inclui a inconsistência real do projeto
# ("assentamento" sem "s" na degradação).
SAD_CSV_DASHBOARD = {
    "desmatamento": {
        "amazoniaLegal": "amazoniaLegal",
        "municipios": "municipios",
        "assentamentos": "assentamentos",
        "terraIndigena": "terra_indigena",
        "unidadeConservacao": "ucs",
    },
    "degradacao": {
        "amazoniaLegal": "amazoniaLegal",
        "municipios": "municipios",
        "assentamentos": "assentamento",
        "terraIndigena": "terra_indigena",
        "unidadeConservacao": "ucs",
    },
}

# Colunas (e ordem) dos CSVs do dashboard, com os nomes que o dashboard usa
# para identificar cada território (MUNICIPIO, TERRA_INDI, UNID_CONSE…).
_SAD_COLUNAS_BASE = ["ALERTA", "MES", "ANO", "SENSOR", "ESTADO", "AREAKM2"]
_SAD_COLUNAS_DASHBOARD = {
    "amazoniaLegal": [],
    "municipios": ["MUNICIPIO"],
    "assentamentos": ["MUNICIPIO", "ASSENTAMEN"],
    "terraIndigena": ["MUNICIPIO", "TERRA_INDI"],
    "unidadeConservacao": ["MUNICIPIO", "UNID_CONSE", "USO", "JURISDICAO"],
}

# A coluna de município muda de nome entre as camadas do arquivo recebido
# (NM_MUN, MUNICIPIOS, MUNICIPIO); em todas as saídas ela vira MUNICIPIO.
_SAD_RENOMEAR = {"NM_MUN": "MUNICIPIO", "MUNICIPIOS": "MUNICIPIO"}
_SAD_RENOMEAR_DASHBOARD = {"TI": "TERRA_INDI", "UC": "UNID_CONSE"}

# Arquivos extraídos do ZIP (o resto, como .qix e .qmd, é ignorado)
_SAD_EXTENSOES = {".shp", ".shx", ".dbf", ".prj", ".cpg", ".geojson"}


@dataclass
class CamadaSAD:
    """Camada (tipo de alerta + recorte territorial) extraída do ZIP."""

    tipo: str  # 'desmatamento' | 'degradacao'
    camada: str  # nome padronizado (valores de _SAD_CAMADAS)
    path: Path  # .shp ou .geojson extraído do ZIP
    reprojetar: bool = False  # True se não estiver em EPSG:4326 (exigido no GeoJSON)


def _importar_libs_geo() -> tuple[Any, Any, Any]:
    """Importa geopandas, pandas e pyogrio, com mensagem clara se faltarem."""
    try:
        import geopandas as gpd
        import pandas as pd
        import pyogrio
    except ImportError as exc:
        raise RuntimeError(
            "geopandas, pandas e pyogrio são necessários para processar o SAD.\n"
            "Instale com: pip install geopandas pandas pyogrio"
        ) from exc
    # O pyogrio registra "Created N records" (INFO) a cada arquivo gravado
    logging.getLogger("pyogrio").setLevel(logging.WARNING)
    return gpd, pd, pyogrio


def identificar_camada_sad(
    nome_arquivo: str,
) -> tuple[str, str, list[tuple[int, int]]] | None:
    """Reconhece tipo, camada e períodos (ano, mês) pelo nome do arquivo.

    alertas_sad_desmatamento_01_2008_07_2026_terrasIndigenas.shp
        → ("desmatamento", "terraIndigena", [(2008, 1), (2026, 7)])

    Retorna None se o nome não seguir o padrão do SAD.
    """
    m = _SAD_NOME_RE.match(Path(nome_arquivo).stem)
    if not m:
        return None
    camada = _SAD_CAMADAS.get(m.group("camada").lower())
    if camada is None:
        return None
    nums = [int(n) for n in m.group("periodos").strip("_").split("_")]
    periodos = [(nums[i + 1], nums[i]) for i in range(0, len(nums), 2)]
    return m.group("tipo").lower(), camada, periodos


def inspecionar_zips_sad(zips: list[Path]) -> dict:
    """Resume o conteúdo dos ZIPs do SAD lendo só a lista de arquivos.

    Sem extrair nada, identifica as camadas encontradas e o período coberto
    pelos nomes. Retorna::

        {"camadas": [(tipo, camada), ...],
         "inicio": (ano, mes) | None, "fim": (ano, mes) | None}
    """
    camadas = set()
    periodos: list[tuple[int, int]] = []
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for nome in zf.namelist():
                if Path(nome).suffix.lower() not in (".shp", ".geojson"):
                    continue
                info = identificar_camada_sad(nome)
                if info:
                    camadas.add((info[0], info[1]))
                    periodos.extend(info[2])
    return {
        "camadas": sorted(camadas),
        "inicio": min(periodos) if periodos else None,
        "fim": max(periodos) if periodos else None,
    }


def _extrair_zips_sad(zips: list[Path], destino: Path) -> list[CamadaSAD]:
    """Extrai as partes relevantes dos ZIPs para uma única pasta.

    O download do Google Drive divide um mesmo shapefile entre vários ZIPs
    (ex.: o .shp num e o .dbf em outro). Retorna as camadas reconhecidas.
    """
    for zip_path in zips:
        with zipfile.ZipFile(zip_path) as zf:
            for info in zf.infolist():
                nome = Path(info.filename).name
                if info.is_dir() or Path(nome).suffix.lower() not in _SAD_EXTENSOES:
                    continue
                with zf.open(info) as origem, open(destino / nome, "wb") as saida:
                    shutil.copyfileobj(origem, saida, 1024 * 1024)

    camadas: list[CamadaSAD] = []
    for arquivo in sorted(destino.iterdir()):
        if arquivo.suffix.lower() not in (".shp", ".geojson"):
            continue
        info = identificar_camada_sad(arquivo.name)
        if info is None:
            logging.warning("Arquivo fora do padrão do SAD, ignorado: %s", arquivo.name)
            continue
        tipo, camada, _periodos = info
        if arquivo.suffix.lower() == ".shp":
            faltando = [
                ext for ext in (".shx", ".dbf") if not arquivo.with_suffix(ext).exists()
            ]
            if faltando:
                raise FileNotFoundError(
                    f"Shapefile {arquivo.name} incompleto "
                    f"(falta {', '.join(faltando)}). Se o download veio dividido "
                    "em várias partes, selecione todos os ZIPs."
                )
        if any(c.tipo == tipo and c.camada == camada for c in camadas):
            logging.warning(
                "Camada %s/%s repetida, ignorando: %s", tipo, camada, arquivo.name
            )
            continue
        camadas.append(CamadaSAD(tipo, camada, arquivo))
    return camadas


def _ler_atributos_sad(camada: CamadaSAD) -> pd.DataFrame:
    """Lê só a tabela de atributos (sem geometria) de uma camada.

    O resultado é indexado pelo FID, com MES/ANO inteiros e a coluna de
    município padronizada.
    """
    _gpd, pd, pyogrio = _importar_libs_geo()
    df = pyogrio.read_dataframe(camada.path, read_geometry=False, fid_as_index=True)

    faltando = [col for col in ("MES", "ANO") if col not in df.columns]
    if faltando:
        raise ValueError(
            f"{camada.path.name} não tem a(s) coluna(s) {', '.join(faltando)}."
        )

    df = df.rename(columns=_SAD_RENOMEAR)
    for col in ("MES", "ANO"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    invalidos = df["MES"].isna() | df["ANO"].isna()
    if invalidos.any():
        logging.warning(
            "%s: %d feição(ões) sem MES/ANO válido ignorada(s).",
            camada.path.name,
            int(invalidos.sum()),
        )
        df = df[~invalidos]
    return df.astype({"MES": int, "ANO": int})


def _csv_dashboard_sad(atributos: pd.DataFrame, camada: str) -> pd.DataFrame:
    """Atributos de uma camada no layout de colunas do CSV do dashboard."""
    df = atributos.rename(columns=_SAD_RENOMEAR_DASHBOARD)
    colunas = _SAD_COLUNAS_BASE + _SAD_COLUNAS_DASHBOARD[camada]
    # Colunas ausentes no arquivo recebido (ex.: a degradação em terra
    # indígena não traz município) ficam vazias.
    return df.reindex(columns=colunas, fill_value="")


def _mesclar_csv_dashboard_sad(
    novo: pd.DataFrame, csv_s3: Path | None, meses: Any
) -> pd.DataFrame:
    """Junta o CSV acumulado do S3 com os dados novos.

    Remove do S3 as linhas dos meses enviados (``meses``, como AAAAMM) e
    acrescenta os novos: os meses enviados são substituídos e o resto do
    histórico é mantido.
    """
    if csv_s3 is None:
        return novo
    _gpd, pd, _pyogrio = _importar_libs_geo()

    antigo = pd.read_csv(csv_s3, dtype=str, keep_default_na=False)
    if "MES" not in antigo.columns or "ANO" not in antigo.columns:
        logging.warning(
            "CSV do S3 sem colunas MES/ANO: será substituído pelos dados do ZIP."
        )
        return novo

    mes = pd.to_numeric(antigo["MES"], errors="coerce")
    ano = pd.to_numeric(antigo["ANO"], errors="coerce")
    # Linhas sem MES/ANO válido (ex.: linhas corrompidas em versões antigas do
    # arquivo) não são lidas pelo dashboard e são descartadas.
    validos = mes.notna() & ano.notna()
    if not validos.all():
        logging.warning(
            "CSV do S3: %d linha(s) sem MES/ANO válido descartada(s).",
            int((~validos).sum()),
        )
    periodo = (ano * 100 + mes).fillna(-1).astype("int64")
    substituir = periodo.isin(meses)
    manter = validos & ~substituir

    antigo = antigo[manter].copy()
    # Versões antigas do arquivo têm MES/ANO como "3.0"; padroniza para inteiro
    antigo["MES"] = mes[manter].astype(int).astype(str)
    antigo["ANO"] = ano[manter].astype(int).astype(str)

    logging.info(
        "CSV do S3: %d linha(s) mantida(s), %d substituída(s) pelos meses enviados.",
        len(antigo),
        int((validos & substituir).sum()),
    )
    if antigo.empty:
        return novo
    antigo = antigo.reindex(columns=novo.columns, fill_value="")
    return pd.concat([antigo, novo], ignore_index=True)


def _nome_csv_dashboard(tipo: str, camada: str) -> str:
    return f"alertas_sad_{tipo}_{SAD_CSV_DASHBOARD[tipo][camada]}.csv"


def _atualizar_dashboard_sad(
    camadas: list[CamadaSAD],
    atributos: dict,
    bucket: str,
    s3_client: Any,
    tmp: Path,
    public: bool,
    mes_unico: tuple[int, int] | None = None,
) -> None:
    """Atualiza os CSVs do dashboard, um por tipo + camada.

    Cada CSV é mesclado com a versão acumulada que já está no S3::

        s3://bucket/dashboard/sad/csv/alertas_sad_{tipo}_{camada}.csv

    mes_unico=(ano, mes) envia só esse mês; None envia todos os dados do ZIP.
    """
    prefixo = s3_dashboard_prefix(DATASETS["sad"].s3_root, "csv")
    for c in camadas:
        nome = _nome_csv_dashboard(c.tipo, c.camada)
        key = prefixo + nome
        df = atributos[(c.tipo, c.camada)]
        if mes_unico:
            ano, mes = mes_unico
            df = df[(df["ANO"] == ano) & (df["MES"] == mes)]
            # Mesmo sem alertas no mês, o mês é substituído no S3
            meses = [ano * 100 + mes]
        else:
            meses = (df["ANO"] * 100 + df["MES"]).unique()
        novo = _csv_dashboard_sad(df, c.camada)

        csv_s3 = tmp / f"s3_{nome}"
        baixou = baixar_arquivo_s3(s3_client, bucket, key, csv_s3)
        if mes_unico and not baixou:
            logging.warning(
                "%s não existe no S3: será criado só com %02d/%d.",
                nome,
                mes_unico[1],
                mes_unico[0],
            )
        final = _mesclar_csv_dashboard_sad(novo, csv_s3 if baixou else None, meses)

        arquivo = tmp / nome
        final.to_csv(arquivo, index=False, encoding="utf-8")
        logging.info("%s: %d linha(s), %d do ZIP.", nome, len(final), len(novo))
        enviar_arquivo_s3(s3_client, arquivo, bucket, key, public)
        csv_s3.unlink(missing_ok=True)
        arquivo.unlink(missing_ok=True)


def _publicar_mes_sad(
    camadas: list[CamadaSAD],
    atributos: dict,
    ano: int,
    mes: int,
    bucket: str,
    s3_client: Any,
    tmp: Path,
    public: bool,
) -> None:
    """Gera e envia os arquivos de download de um mês.

    Um ZIP por formato, com um arquivo por tipo + camada
    (alertas_sad_{tipo}_{MM}_{AAAA}_{camada}.{ext})::

        s3://bucket/sad/{geojson,csv,shapefile}/sad_{AAAA}_{MM}.zip
    """
    gpd, _pd, pyogrio = _importar_libs_geo()
    stem = f"sad_{ano}_{mes:02d}"
    dir_mes = tmp / stem
    dirs = {fmt: dir_mes / fmt for fmt in FORMATS}
    for d in dirs.values():
        d.mkdir(parents=True, exist_ok=True)

    alertas = 0
    for c in camadas:
        df = atributos[(c.tipo, c.camada)]
        sel = df[(df["ANO"] == ano) & (df["MES"] == mes)]
        if sel.empty:
            continue

        # Lê do arquivo só as geometrias das feições do mês, pelo FID
        geom = pyogrio.read_dataframe(
            c.path, fids=sel.index.to_numpy(), columns=[], fid_as_index=True
        )
        gdf = gpd.GeoDataFrame(sel.loc[geom.index], geometry=geom.geometry)

        nome = f"alertas_sad_{c.tipo}_{mes:02d}_{ano}_{c.camada}"
        pyogrio.write_dataframe(
            gdf, dirs["shapefile"] / f"{nome}.shp", encoding="UTF-8"
        )
        pyogrio.write_dataframe(
            gdf.to_crs(epsg=4326) if c.reprojetar else gdf,
            dirs["geojson"] / f"{nome}.geojson",
            driver="GeoJSON",
        )
        sel.to_csv(dirs["csv"] / f"{nome}.csv", index=False, encoding="utf-8")
        alertas += len(sel)

    if alertas == 0:
        logging.warning(
            "Nenhum alerta de %02d/%d no ZIP: arquivos mensais não gerados.", mes, ano
        )
    else:
        for fmt in FORMATS:
            arquivo = dir_mes / f"{stem}_{fmt}.zip"
            zipar_diretorio(dirs[fmt], arquivo)
            key = f"{DATASETS['sad'].s3_root}/{fmt}/{stem}.zip"
            enviar_arquivo_s3(s3_client, arquivo, bucket, key, public)
        logging.info("SAD %02d/%d: %d alerta(s) publicados.", mes, ano, alertas)
    shutil.rmtree(dir_mes, ignore_errors=True)


def _log_dry_run_sad(
    resumo: dict,
    bucket: str,
    ano: int | None,
    mes: int | None,
    todos_meses: bool,
    public: bool,
    dashboard_todos_meses: bool,
) -> None:
    prefixo_dashboard = s3_dashboard_prefix(DATASETS["sad"].s3_root, "csv")
    s3_root = DATASETS["sad"].s3_root
    substituidos = "os meses do ZIP" if dashboard_todos_meses else f"{mes:02d}/{ano}"
    for tipo, camada in resumo["camadas"]:
        key = prefixo_dashboard + _nome_csv_dashboard(tipo, camada)
        logging.info(
            "[DRY RUN] Baixaria s3://%s/%s, substituiria %s e reenviaria",
            bucket,
            key,
            substituidos,
        )
    if todos_meses:
        (ano_ini, mes_ini), (ano_fim, mes_fim) = resumo["inicio"], resumo["fim"]
        logging.info(
            "[DRY RUN] Enviaria s3://%s/%s/{geojson,csv,shapefile}/sad_AAAA_MM.zip "
            "para cada mês com alertas entre %02d/%d e %02d/%d",
            bucket,
            s3_root,
            mes_ini,
            ano_ini,
            mes_fim,
            ano_fim,
        )
    else:
        for fmt in FORMATS:
            logging.info(
                "[DRY RUN] Enviaria s3://%s/%s/%s/sad_%d_%02d.zip",
                bucket,
                s3_root,
                fmt,
                ano,
                mes,
            )
    if public:
        logging.info("[DRY RUN] Definiria ACL public-read em todos os objetos enviados")


def processar_sad_zip(
    zips: list[Path],
    bucket: str,
    dry_run: bool,
    ano: int | None = None,
    mes: int | None = None,
    todos_meses: bool = False,
    public: bool = True,
    dashboard_todos_meses: bool = True,
    banco: str | None = None,
) -> None:
    """Atualiza o SAD a partir do ZIP recebido.

    O ZIP traz um shapefile (ou GeoJSON) acumulado por tipo + camada, ex.::

        shapefile/alertas_sad_desmatamento_01_2008_07_2026_municipios.shp

    Aceita vários ZIPs quando o download vem dividido em partes.

    1. Dashboard — CSV de atributos de cada tipo + camada, mesclado com o
       acumulado do S3 (os meses enviados são substituídos)::

           s3://bucket/dashboard/sad/csv/alertas_sad_{tipo}_{camada}.csv

       Envia todos os dados do ZIP ou, com dashboard_todos_meses=False, só
       o mês ano/mes.
    2. Download — um ZIP por mês e formato, com um arquivo por tipo + camada::

           s3://bucket/sad/{geojson,csv,shapefile}/sad_{AAAA}_{MM}.zip

       Só para o mês ano/mes (se omitidos, o último mês do nome dos
       arquivos) ou, com todos_meses=True, para cada mês com alertas no ZIP.

    Com ``banco`` ('real' ou 'simulation', ver banco.carga_sad.modo_banco),
    os alertas de todos os meses do ZIP são gravados antes no banco
    (imazongeo.sad_alerta); se a gravação falhar, nada é enviado ao S3.
    """
    zips = [Path(z) for z in zips]
    if not zips:
        raise ValueError("Nenhum ZIP do SAD informado.")
    for zip_path in zips:
        if not zip_path.is_file():
            raise FileNotFoundError(f"ZIP do SAD não encontrado: {zip_path}")

    resumo = inspecionar_zips_sad(zips)
    if not resumo["camadas"]:
        raise ValueError(
            "Nenhuma camada do SAD encontrada no(s) ZIP(s). Esperado: "
            "alertas_sad_{tipo}_{MM}_{AAAA}_..._{camada}.shp"
        )
    if (not todos_meses or not dashboard_todos_meses) and (ano is None or mes is None):
        ano, mes = resumo["fim"]

    logging.info("=== SAD: %s ===", ", ".join(z.name for z in zips))
    logging.info("Camadas: %s", ", ".join(f"{t}/{c}" for t, c in resumo["camadas"]))
    logging.info(
        "CSVs do dashboard: %s",
        "todos os dados do ZIP" if dashboard_todos_meses else f"só {mes:02d}/{ano}",
    )
    logging.info(
        "Arquivos mensais: %s",
        "todos os meses do ZIP" if todos_meses else f"{mes:02d}/{ano}",
    )

    if dry_run:
        _log_dry_run_sad(
            resumo, bucket, ano, mes, todos_meses, public, dashboard_todos_meses
        )
        return

    prefixo_dashboard = s3_dashboard_prefix(DATASETS["sad"].s3_root, "csv")
    s3_root = DATASETS["sad"].s3_root
    _gpd, _pd, pyogrio = _importar_libs_geo()
    s3_client = create_s3_client()

    with tempfile.TemporaryDirectory(
        prefix="imazon_sad_", ignore_cleanup_errors=True
    ) as tmp_str:
        tmp = Path(tmp_str)
        dir_extraido = tmp / "extraido"
        dir_extraido.mkdir()
        logging.info("Extraindo ZIP(s) em %s …", dir_extraido)
        camadas = _extrair_zips_sad(zips, dir_extraido)

        if banco:
            from .banco.carga_sad import arquivo_da_camada, gravar

            logging.info(">>> Banco de dados (%s)", banco)
            gravar([arquivo_da_camada(c) for c in camadas], banco)

        atributos = {}
        for c in camadas:
            c.reprojetar = pyogrio.read_info(c.path)["crs"] not in (None, "EPSG:4326")
            atributos[(c.tipo, c.camada)] = _ler_atributos_sad(c)
            logging.info(
                "%s/%s: %d feição(ões)",
                c.tipo,
                c.camada,
                len(atributos[(c.tipo, c.camada)]),
            )

        logging.info(">>> Dashboard: s3://%s/%s", bucket, prefixo_dashboard)
        _atualizar_dashboard_sad(
            camadas,
            atributos,
            bucket,
            s3_client,
            tmp,
            public,
            mes_unico=None if dashboard_todos_meses else (ano, mes),
        )

        if todos_meses:
            meses = sorted(
                {
                    (int(a), int(m))
                    for df in atributos.values()
                    for a, m in df[["ANO", "MES"]]
                    .drop_duplicates()
                    .itertuples(index=False)
                }
            )
        else:
            meses = [(ano, mes)]

        logging.info(
            ">>> Download: %d mês(es) em s3://%s/%s/{geojson,csv,shapefile}/",
            len(meses),
            bucket,
            s3_root,
        )
        for i, (a, m) in enumerate(meses, start=1):
            logging.info("[%d/%d] SAD %02d/%d", i, len(meses), m, a)
            _publicar_mes_sad(camadas, atributos, a, m, bucket, s3_client, tmp, public)
