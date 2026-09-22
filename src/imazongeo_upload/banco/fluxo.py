"""Fluxo fonte → banco → S3.

1. :func:`processar_envio`: valida o arquivo enviado, grava o período no banco
   (substituindo o anterior), gera GeoJSON/CSV/Shapefile a partir do banco e
   publica no S3, em seguida e automaticamente.
2. :func:`republicar`: gera e publica de novo, a partir do banco, períodos já
   gravados (ex.: todo o histórico no padrão novo, ou após falha no S3).
3. :func:`importar_legado`: grava no banco os arquivos legados do S3 baixados
   para o espelho local (:mod:`.espelho`).

Modos (os mesmos da aplicação):

- ``dry_run``: só valida e lista o que seria feito; não abre o banco nem o S3;
- ``simulation``: faz tudo numa transação desfeita no final, com o cliente S3
  substituído (``usar_cliente_s3``); o banco e o S3 reais não mudam;
- ``real``: grava no banco (commit) e publica no S3.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from ..s3 import create_s3_client
from . import db, espelho
from .entrada import Envio, preparar_envio, sha256_arquivo
from .exportacao import ArquivoGerado, escrever_arquivos
from .padrao import Periodo, padrao

MODOS = ("dry_run", "simulation", "real")
CONTENT_TYPE = {
    "geojson": "application/geo+json",
    "csv": "text/csv; charset=utf-8",
    "shapefile": "application/zip",
}


@dataclass
class Resultado:
    modo: str
    envio: Envio | None = None
    carga_id: int | None = None
    publicados: list[str] = field(default_factory=list)  # chaves S3


def _cliente_s3(modo: str):
    cliente = create_s3_client()
    if modo == "simulation" and type(cliente).__module__.startswith("botocore"):
        raise RuntimeError("Simulação sem cliente S3 substituto: nada foi enviado.")
    return cliente


def _enviar_s3(
    cliente, arquivos: list[ArquivoGerado], bucket: str, public: bool
) -> list[tuple[ArquivoGerado, str]]:
    enviados = []
    for a in arquivos:
        sha = sha256_arquivo(a.caminho)
        logging.info("Enviando %s → s3://%s/%s", a.caminho.name, bucket, a.chave_s3)
        cliente.upload_file(
            str(a.caminho),
            bucket,
            a.chave_s3,
            ExtraArgs={"ContentType": CONTENT_TYPE[a.formato]},
        )
        if public:
            cliente.put_object_acl(Bucket=bucket, Key=a.chave_s3, ACL="public-read")
        enviados.append((a, sha))
    return enviados


def _publicar(
    conn,
    carga_id: int,
    arquivos: list[ArquivoGerado],
    bucket: str,
    modo: str,
    public: bool,
) -> list[str]:
    """Envia ao S3; no modo real, registra cada publicação no banco."""
    enviados = _enviar_s3(_cliente_s3(modo), arquivos, bucket, public)
    if modo == "real":
        with conn, conn.cursor() as cur:
            for a, sha in enviados:
                db.registrar_publicacao(
                    cur, carga_id, a.formato, bucket, a.chave_s3, a.caminho, sha, public
                )
    return [a.chave_s3 for a, _ in enviados]


def _previa(envio: Envio, bucket: str) -> None:
    p = envio.padrao
    logging.info(
        "[PRÉVIA] Gravaria %d registros de %s %s no banco (substituindo o período)",
        envio.registros,
        p.slug,
        envio.periodo,
    )
    for formato in p.formatos:
        chave = p.chave_s3(envio.periodo, formato)
        logging.info("[PRÉVIA] Publicaria s3://%s/%s", bucket, chave)


def processar_envio(
    caminho: Path,
    dataset: str,
    periodo: Periodo,
    *,
    bucket: str,
    modo: str = "dry_run",
    public: bool = False,
    nome: str | None = None,
    database_url: str | None = None,
    destino: Path | None = None,
) -> Resultado:
    """Envio → banco → S3.

    ``destino`` guarda os arquivos gerados (se omitido, pasta temporária).
    """
    if modo not in MODOS:
        raise ValueError(f"Modo inválido: {modo}")
    envio = preparar_envio(caminho, dataset, periodo, nome)
    if modo == "dry_run":
        _previa(envio, bucket)
        return Resultado(modo, envio)

    p = envio.padrao
    with (
        tempfile.TemporaryDirectory(prefix="imazon_export_") as tmp,
        db.conectar(database_url) as conn,
    ):
        pasta = destino or Path(tmp)
        try:
            with conn.cursor() as cur:
                carga_id = db.gravar_envio(cur, envio)
            # Lido de volta do banco (na mesma transação): o S3 recebe o que foi gravado
            arquivos = escrever_arquivos(
                db.ler_periodo(conn, p, periodo), p, periodo, pasta
            )
            if modo == "simulation":
                publicados = _publicar(conn, carga_id, arquivos, bucket, modo, public)
                conn.rollback()
                logging.info(
                    "[SIMULAÇÃO] Transação desfeita: o banco não foi alterado."
                )
                return Resultado(modo, envio, None, publicados)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        logging.info(
            "Banco atualizado: %s (carga %d). Publicando no S3...",
            envio.resumo(),
            carga_id,
        )
        try:
            publicados = _publicar(conn, carga_id, arquivos, bucket, modo, public)
        except Exception:
            logging.error(
                "O banco foi atualizado, mas a publicação no S3 falhou. Depois de "
                "corrigir, rode: imazongeo-banco republicar --dataset %s --ano %d%s",
                p.slug,
                periodo.ano,
                f" --trimestre {periodo.trimestre}" if periodo.trimestre else "",
            )
            raise
    return Resultado(modo, envio, carga_id, publicados)


def republicar(
    dataset: str,
    periodos: list[Periodo] | None,
    *,
    bucket: str,
    modo: str = "dry_run",
    public: bool = False,
    database_url: str | None = None,
) -> list[str]:
    """Gera e publica a partir do banco os períodos informados (None = todos)."""
    if modo not in MODOS:
        raise ValueError(f"Modo inválido: {modo}")
    p = padrao(dataset)
    publicados: list[str] = []
    with db.conectar(database_url) as conn:
        periodos = periodos or db.periodos_vigentes(conn, dataset)
        for periodo in periodos:
            with conn.cursor() as cur:
                vigente = db.carga_vigente(cur, dataset, periodo)
            conn.rollback()
            if vigente is None:
                raise ValueError(f"{dataset} {periodo} não está no banco.")
            if modo == "dry_run":
                for formato in p.formatos:
                    logging.info(
                        "[PRÉVIA] Publicaria s3://%s/%s",
                        bucket,
                        p.chave_s3(periodo, formato),
                    )
                continue
            with tempfile.TemporaryDirectory(prefix="imazon_export_") as tmp:
                df = db.ler_periodo(conn, p, periodo)
                conn.rollback()
                arquivos = escrever_arquivos(df, p, periodo, Path(tmp))
                publicados += _publicar(
                    conn, vigente[0], arquivos, bucket, modo, public
                )
    return publicados


def importar_legado(
    destino: Path,
    datasets: list[str],
    ano_inicial: int = 1985,
    ano_final: int = 2100,
    forcar: bool = False,
    database_url: str | None = None,
) -> dict[str, int]:
    """Grava no banco os arquivos legados do espelho local (sem publicar no S3).

    Períodos cuja carga vigente veio do mesmo arquivo (mesmo SHA-256) são
    pulados, a menos que ``forcar``. Períodos já gravados a partir de um envio
    pela aplicação nunca são sobrescritos pelo legado.
    """
    arquivos = [
        a
        for a in espelho.ler_manifesto(destino)
        if a.dataset in datasets and ano_inicial <= a.periodo.ano <= ano_final
    ]
    if not arquivos:
        logging.warning(
            "Manifesto vazio em %s. Rode 'imazongeo-banco baixar-legado'.", destino
        )
        return {}
    resumo: dict[str, int] = {}
    falhas = []
    with db.conectar(database_url) as conn:
        for a in espelho.escolher_por_periodo(arquivos):
            caminho = destino / a.chave
            with conn, conn.cursor() as cur:
                cur.execute(
                    """SELECT origem, sha256 FROM imazongeo.carga WHERE dataset = %s
                       AND vigente AND ano = %s
                       AND trimestre IS NOT DISTINCT FROM %s""",
                    (a.dataset, a.periodo.ano, a.periodo.trimestre),
                )
                vigente = cur.fetchone()
            if vigente and vigente[0] == "envio":
                logging.info(
                    "%s %s: já enviado pela aplicação; legado ignorado",
                    a.dataset,
                    a.periodo,
                )
                continue
            if vigente and not forcar and vigente[1] == sha256_arquivo(caminho):
                logging.info("%s %s: sem mudanças", a.dataset, a.periodo)
                continue
            try:
                envio = preparar_envio(caminho, a.dataset, a.periodo, nome=a.chave)
                with conn, conn.cursor() as cur:
                    db.gravar_envio(cur, envio, origem="s3_legado")
            except Exception as e:  # noqa: BLE001 - segue com os demais períodos
                logging.error("Falha ao importar %s: %s", a.chave, e)
                falhas.append(f"{a.chave}: {e}")
                continue
            resumo[a.dataset] = resumo.get(a.dataset, 0) + envio.registros
    if falhas:
        raise RuntimeError(
            f"{len(falhas)} arquivo(s) não importado(s):\n" + "\n".join(falhas)
        )
    return resumo
