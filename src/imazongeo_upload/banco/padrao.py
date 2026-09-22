"""Padrão de dados do ImazonGeo: campos, períodos e nomes de arquivo.

O banco é a fonte dos dados. Os arquivos publicados no S3 (GeoJSON, CSV e
Shapefile) são gerados a partir dele com os mesmos campos, na mesma ordem e
com os mesmos nomes em todos os formatos e anos. Os nomes têm no máximo 10
caracteres (limite do Shapefile) e são iguais às colunas das tabelas.

Arquivos de download no S3::

    {raiz}/{formato}/{raiz}_{periodo}.{ext}

    simex/geojson/simex_2024.geojson
    ameaca_e_pressao/csv/ameaca_e_pressao_2025_t3.csv
    floreser/shapefile/floreser_2024.zip

``periodo`` é ``AAAA`` (anual), ``AAAA_tN`` (trimestral) ou ``AAAA_MM`` (mensal).
"""

from __future__ import annotations

from dataclasses import dataclass

SRID = 4326  # WGS 84, 2D; todas as geometrias são MultiPolygon

# formato -> (pasta no S3, extensão)
FORMATOS = {
    "geojson": ("geojson", "geojson"),
    "csv": ("csv", "csv"),
    "shapefile": ("shapefile", "zip"),
}


@dataclass(frozen=True)
class Periodo:
    ano: int
    trimestre: int | None = None
    mes: int | None = None

    def __post_init__(self) -> None:
        if not 1900 <= self.ano <= 2100:
            raise ValueError(f"Ano inválido: {self.ano}")
        if self.trimestre is not None and not 1 <= self.trimestre <= 4:
            raise ValueError(f"Trimestre inválido: {self.trimestre}")
        if self.mes is not None and not 1 <= self.mes <= 12:
            raise ValueError(f"Mês inválido: {self.mes}")

    @property
    def sufixo(self) -> str:
        """Parte do nome do arquivo: 2024, 2025_t3 ou 2025_09."""
        if self.mes:
            return f"{self.ano}_{self.mes:02d}"
        if self.trimestre:
            return f"{self.ano}_t{self.trimestre}"
        return str(self.ano)

    def __str__(self) -> str:
        if self.mes:
            return f"{self.ano}-{self.mes:02d}"
        if self.trimestre:
            return f"{self.ano}-T{self.trimestre}"
        return str(self.ano)


@dataclass(frozen=True)
class Campo:
    nome: str
    tipo: str  # 'int', 'float' ou 'str'
    descricao: str


@dataclass(frozen=True)
class Padrao:
    slug: str
    nome: str
    periodicidade: str  # 'anual', 'trimestral' ou 'mensal'
    raiz_s3: str
    campos: tuple[Campo, ...]
    geometria: bool

    @property
    def nomes(self) -> list[str]:
        return [c.nome for c in self.campos]

    def validar_periodo(self, periodo: Periodo) -> None:
        trimestral = periodo.trimestre is not None
        mensal = periodo.mes is not None
        esperado = {
            "anual": not trimestral and not mensal,
            "trimestral": trimestral and not mensal,
            "mensal": mensal and not trimestral,
        }[self.periodicidade]
        if not esperado:
            raise ValueError(
                f"{self.slug} é {self.periodicidade}; período inválido: {periodo}"
            )

    @property
    def formatos(self) -> tuple[str, ...]:
        return ("geojson", "csv", "shapefile") if self.geometria else ("csv",)

    def chave_s3(self, periodo: Periodo, formato: str) -> str:
        pasta, ext = FORMATOS[formato]
        return f"{self.raiz_s3}/{pasta}/{self.raiz_s3}_{periodo.sufixo}.{ext}"


# Camadas de cada arquivo anual do SIMEX. A camada 'municipios' cobre toda a
# exploração do ano; as demais são recortes dela por categoria fundiária.
CAMADAS_SIMEX = (
    "municipios",
    "imoveis_rurais",
    "assentamentos",
    "terras_indigenas",
    "unidades_conservacao",
    "terras_nao_destinadas",
)
CATEGORIAS_SIMEX = ("autorizada", "não autorizada", "análise")

# Grupos de cada trimestre de Ameaça & Pressão (top 10 de cada)
RECORTES_AP = ("geral", "categ_ti", "categ_uce", "categ_ucf")
CLASSES_AP = ("ameaca", "pressao")
MODALIDADES_AP = ("ti", "ucf", "uce")

SIMEX = Padrao(
    slug="simex",
    nome="SIMEX - Sistema de Monitoramento da Exploração Madeireira",
    periodicidade="anual",
    raiz_s3="simex",
    geometria=True,
    campos=(
        Campo("ano", "int", "Ano da exploração"),
        Campo("camada", "str", "Recorte territorial: " + ", ".join(CAMADAS_SIMEX)),
        Campo(
            "categoria", "str", "autorizada, não autorizada ou análise (vazio em 2020)"
        ),
        Campo("uf", "str", "Sigla da UF"),
        Campo("municipio", "str", "Nome do município"),
        Campo("cod_mun", "int", "Código IBGE do município (7 dígitos)"),
        Campo("territorio", "str", "Nome do imóvel, TI, UC, assentamento ou gleba"),
        Campo(
            "subclasse",
            "str",
            "Classe fundiária de origem (SIGEF, CARpr, ARU, ND_B...)",
        ),
        Campo("area_ha", "float", "Área do polígono (ha), calculada da geometria"),
    ),
)

AMEACA_PRESSAO = Padrao(
    slug="ameaca_pressao",
    nome="Ameaça e Pressão de Desmatamento em Áreas Protegidas",
    periodicidade="trimestral",
    raiz_s3="ameaca_e_pressao",
    geometria=True,
    campos=(
        Campo("ano", "int", "Ano"),
        Campo("trimestre", "int", "Trimestre (1 a 4)"),
        Campo("legenda", "str", "Meses cobertos, ex.: julho a setembro"),
        Campo("recorte", "str", "Ranking: " + ", ".join(RECORTES_AP)),
        Campo("classe", "str", "ameaca ou pressao"),
        Campo("posicao", "int", "Posição no ranking"),
        Campo("celulas", "int", "Células com desmatamento"),
        Campo("nome", "str", "Nome da área protegida"),
        Campo("modalidade", "str", "ti, ucf (UC federal) ou uce (UC estadual)"),
        Campo("categoria", "str", "Categoria da área, ex.: Floresta Nacional"),
        Campo("uso", "str", "Uso Sustentável, Proteção Integral ou Terra Indígena"),
        Campo("jurisdicao", "str", "Federal ou Estadual"),
        Campo("estado", "str", "UF(s), ex.: AM/PA"),
    ),
)

FLORESER = Padrao(
    slug="floreser",
    nome="FloreSer - Vegetação Secundária por Município e Idade",
    periodicidade="anual",
    raiz_s3="floreser",
    geometria=False,
    campos=(
        Campo("ano", "int", "Ano"),
        Campo("cod_uf", "int", "Código IBGE da UF"),
        Campo("estado", "str", "Nome da UF"),
        Campo("cod_mun", "int", "Código IBGE do município (7 dígitos)"),
        Campo("municipio", "str", "Nome do município"),
        Campo("idade", "int", "Idade da vegetação secundária (anos)"),
        Campo("area_ha", "float", "Área de vegetação secundária (ha)"),
    ),
)

TIPOS_SAD = ("desmatamento", "degradacao")
CAMADAS_SAD = (
    "amazonia_legal",
    "municipios",
    "assentamentos",
    "terras_indigenas",
    "unidades_conservacao",
)

# Campos da visão vw_sad, lida pelo dashboard do SAD
SAD = Padrao(
    slug="sad",
    nome="SAD - Sistema de Alerta de Desmatamento",
    periodicidade="mensal",
    raiz_s3="sad",
    geometria=True,
    campos=(
        Campo("ano", "int", "Ano do alerta"),
        Campo("mes", "int", "Mês do alerta (1 a 12)"),
        Campo("tipo", "str", "desmatamento ou degradacao"),
        Campo("camada", "str", "Recorte territorial: " + ", ".join(CAMADAS_SAD)),
        Campo("sensor", "str", "Sensor da imagem, ex.: Sentinel-2"),
        Campo("uf", "str", "Sigla da UF"),
        Campo("municipio", "str", "Nome do município"),
        Campo("territorio", "str", "Nome do assentamento, TI ou UC"),
        Campo("uso", "str", "Uso da UC: Uso Sustentável ou Proteção Integral"),
        Campo("jurisdicao", "str", "Jurisdição da UC: Federal ou Estadual"),
        Campo("area_km2", "float", "Área do alerta (km²)"),
    ),
)

# Datasets com o fluxo genérico envio → banco → S3 (entrada.py, fluxo.py).
# O SAD tem fluxo próprio (sad.py): cada envio traz vários meses.
PADROES = {p.slug: p for p in (SIMEX, AMEACA_PRESSAO, FLORESER)}
# Todos os datasets com tabelas no banco
TODOS = {**PADROES, SAD.slug: SAD}


def padrao(slug: str) -> Padrao:
    try:
        return TODOS[slug]
    except KeyError:
        raise ValueError(f"Dataset sem padrão no banco: {slug}") from None
