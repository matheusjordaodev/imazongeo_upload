"""Publicação dos datasets do ImazonGeo no AWS S3.

Datasets suportados: SAD (mensal), Floreser (anual), Ameaça & Pressão
(trimestral) e SIMEX (anual). Cada um é publicado em três formatos
(shapefile, csv e geojson) para download e, opcionalmente, nos arquivos
lidos pelos dashboards.
"""

__version__ = "1.0.0"
