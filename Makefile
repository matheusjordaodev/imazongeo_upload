# Atalhos de desenvolvimento (Ubuntu). Ex.: make dev && make check
VENV ?= .venv
PY := $(VENV)/bin/python
SRC := src tests scripts deploy

.PHONY: install dev lint format test check web

$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install --upgrade pip

install: $(PY)  ## Instala o pacote com a interface web
	$(PY) -m pip install -e ".[web]"

dev: $(PY)  ## Instala também as ferramentas de desenvolvimento
	$(PY) -m pip install -e ".[dev]"

lint:  ## Verifica PEP 8 / imports / formatação (ruff)
	$(VENV)/bin/ruff check $(SRC)
	$(VENV)/bin/ruff format --check $(SRC)

format:  ## Corrige e formata o código
	$(VENV)/bin/ruff check --fix $(SRC)
	$(VENV)/bin/ruff format $(SRC)

test:  ## Roda os testes
	$(PY) -m pytest

check: lint test

web:  ## Servidor web de desenvolvimento (http://127.0.0.1:5000)
	$(PY) -m imazongeo_upload.web
