"""Banco de dados PostgreSQL/PostGIS como fonte dos datasets do ImazonGeo.

Fluxo: arquivo enviado → validação e padronização (:mod:`.entrada`) → banco
(:mod:`.db`) → GeoJSON/CSV/Shapefile gerados do banco (:mod:`.exportacao`) → S3
(:mod:`.fluxo`). Campos e nomes de arquivo seguem :mod:`.padrao`.
"""
