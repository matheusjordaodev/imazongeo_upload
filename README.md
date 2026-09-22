# ImazonGeo Upload

Publicação dos datasets do ImazonGeo no AWS S3, para download e para os
dashboards:

| Dataset | Frequência | Entrada |
|---|---|---|
| **SAD** | mensal | ZIP recebido com os alertas acumulados (um arquivo por tipo + camada) |
| **Floreser** | anual | arquivo do ano |
| **Ameaça & Pressão** | trimestral | arquivo do trimestre |
| **SIMEX** | anual | arquivo unificado do ano |

Cada dataset é publicado em três formatos: `shapefile` (ZIP), `csv` e
`geojson`. Há quatro formas de uso, todas sobre o mesmo pacote Python:

- **linha de comando** (`imazongeo-upload`), para scripts e VMs;
- **simulador** (`imazongeo-simulador`), que roda o fluxo completo num S3 local, sem credenciais;
- **interface web** (Flask + gunicorn), para uso numa VM Ubuntu;
- **interface gráfica** (Tkinter), para uso local e para gerar o `.exe` do Windows.

## Estrutura

```text
src/imazongeo_upload/
  config.py          .env, região AWS e logging
  datasets.py        datasets, períodos, nomes de arquivo e prefixos S3
  s3.py              cliente S3, download e upload
  concat.py          junção dos arquivos locais com os já publicados no S3
  sad.py             fluxo do SAD (dashboard + arquivos mensais)
  processing.py      fluxo genérico de Floreser / A&P / SIMEX
  ameaca_pressao.py  dashboard de Ameaça & Pressão
  simex.py           dashboard do SIMEX
  simulador.py       SimuladorS3 e comando imazongeo-simulador
  cli.py             comando imazongeo-upload
  gui.py             interface gráfica (Tkinter)
  web/               interface web (Flask): server.py, conversion.py, templates/, static/
tests/               testes (pytest)
deploy/              instalação em VM Ubuntu: script, systemd, gunicorn e nginx
scripts/             diagnósticos do SIMEX e build do .exe (scripts/windows/)
```

## Requisitos

- Ubuntu 22.04 ou 24.04 (Python 3.10 ou superior). As bibliotecas
  geoespaciais (GDAL, PROJ) vêm nos pacotes do pip: não é preciso instalar
  nada do sistema além de `python3-venv`.
- A interface gráfica precisa também de `sudo apt install python3-tk`.

## Instalação para desenvolvimento

```bash
sudo apt install python3-venv make
make dev          # cria .venv e instala o pacote em modo editável com as ferramentas
make check        # lint (PEP 8) + testes
```

Sem `make`:

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Configuração (`.env`)

Copie `.env.example` para `.env` e preencha. O `.env` é procurado nesta
ordem: variável `IMAZON_ENV_FILE`, `.env` no diretório atual e `.env` na
raiz do repositório. Variáveis já definidas no ambiente têm prioridade.

| Variável | Uso |
|---|---|
| `ACCESS_KEY`, `PRIVATE_KEY` | credenciais AWS (upload real) |
| `AWS_REGION` | região (ou `AWS_DEFAULT_REGION`; padrão `us-east-1`) |
| `UPLOAD_PASSWORD` | senha pedida pela GUI e pela web antes do upload real |
| `WEB_HOST`, `WEB_PORT` | endereço da interface web (padrão `127.0.0.1:5000`) |
| `WEB_ALLOWED_HOSTS` | valores aceitos no cabeçalho `Host`, separados por vírgula (padrão `127.0.0.1:PORTA,localhost:PORTA`) |
| `WEB_SECRET_KEY` | chave fixa das sessões (opcional) |

As credenciais precisam de `s3:PutObject` e `s3:GetObject` no bucket e,
para objetos públicos (`--public`), `s3:PutObjectAcl`. O bucket não pode
bloquear ACLs públicas se `--public` for usado.

## Linha de comando

Por padrão tudo roda em **dry-run** (só mostra o que faria). Use
`--no-dry-run` para enviar de verdade.

```bash
# SAD: prévia a partir do ZIP recebido
imazongeo-upload sad --bucket imazongeo3-web --zip alertas.zip

# SAD: upload real e público, download dividido em partes,
# arquivos mensais de todos os meses do ZIP
imazongeo-upload sad --bucket imazongeo3-web --zip parte1.zip --zip parte2.zip \
    --todos-meses --no-dry-run --public

# Floreser / SIMEX / Ameaça & Pressão a partir da pasta dados/
imazongeo-upload simex --bucket imazongeo3-web --year 2025 --no-dry-run
imazongeo-upload ameaca_pressao --bucket imazongeo3-web --year 2025 --quarter 3 --no-dry-run --public

# Simulação completa num S3 local (sem credenciais)
imazongeo-simulador sad --zip alertas.zip --storage-dir /tmp/s3-simulado
```

`python -m imazongeo_upload` equivale a `imazongeo-upload`. Veja todas as
opções com `--help`.

Para Floreser, SIMEX e Ameaça & Pressão, os arquivos são procurados em
`--base-dir` (padrão `dados/`):

```text
dados/
  floreser/2025/{shapefile,csv,geojson}/floreser_2025.{zip,csv,geojson}
  simex/2025/{shapefile,csv,geojson}/simex_unificado_2025.{zip,csv,geojson}
  ameaca_pressao/2025/T3/{shapefile,csv,geojson}/ameaca_e_pressao_3_trimestre_2025.{zip,csv,geojson}
```

## Interface web

```bash
make web          # ou: .venv/bin/python -m imazongeo_upload.web
```

Abra <http://127.0.0.1:5000>. Os modos são **prévia** (sem envio),
**simulação** (S3 local temporário, apagado ao final) e **upload real**
(pede `UPLOAD_PASSWORD` e confirmação). As credenciais nunca são enviadas
ao navegador. O servidor executa um processamento por vez, aceita até
1 GB por envio e mantém em memória os logs dos últimos 20 processamentos.

Regras de entrada:

- **Todas as bases** recebem um ZIP com arquivos `.geojson`
  (FeatureCollections não vazias); ZIPs com Shapefile ou CSV são recusados.
  GeoJSON, CSV e Shapefile são gerados automaticamente e todas as
  conversões terminam antes do primeiro upload. Nomes de campo do
  Shapefile ficam limitados a 10 caracteres.
- **SAD**: GeoJSONs com polígonos e propriedades `ANO` e `MES` inteiras,
  um por tipo/camada. Nomes no padrão
  `alertas_sad_{tipo}_{MM}_{AAAA}_{camada}.geojson` são reconhecidos
  automaticamente; para nomes livres, escolha o tipo e a camada na tela.
  *Gerar Download para diretório espacial* publica
  `sad/{shapefile,csv,geojson}/sad_AAAA_MM.zip` para todos os meses
  presentes; desmarcada, só o mês selecionado. O dashboard
  (`dashboard/sad/csv/`) é atualizado conforme a opção própria.
- **Floreser e SIMEX**: todos os GeoJSONs do ZIP são reunidos numa base do
  período informado.
- **Ameaça & Pressão**: os atributos `ANO` e `TRIMESTRE` (ou `MES`,
  convertido em trimestre) filtram o período selecionado; sem eles, o
  arquivo é considerado do período da tela. No dashboard, envie um GeoJSON
  por categoria com o nome
  `ameaca_e_pressao_{AAAA}_{categoria}_{ameaca|pressao}.geojson`: no
  arquivo existente, só o trimestre selecionado é substituído, e histórico
  sem período identificável bloqueia a atualização.
- Geometrias inválidas de SIMEX, Floreser e A&P são reparadas com
  `make_valid` (o log informa quantas); nenhum registro é descartado, e
  geometrias ausentes, vazias ou não poligonais interrompem a operação.

Downloads vão para `{base}/{geojson,csv,shapefile}/` e o dashboard para
`dashboard/{base}/{geojson,csv,shapefile}/`, mesclado com o histórico já
publicado. O upload real pode substituir dados existentes do mesmo período.

## Deploy numa VM Ubuntu

O script instala a CLI, o simulador e a interface web como serviço
systemd (gunicorn), com usuário próprio e isolamento de sistema de
arquivos:

```bash
git clone https://github.com/matheusjordaodev/imazongeo_upload.git
cd imazongeo_upload
sudo ./deploy/install_ubuntu.sh
sudo nano /etc/imazongeo-upload/imazongeo-upload.env   # credenciais e senha
sudo systemctl restart imazongeo-upload
```

| O quê | Onde |
|---|---|
| código e venv | `/opt/imazongeo-upload` |
| configuração | `/etc/imazongeo-upload/imazongeo-upload.env` |
| serviço | `systemctl status imazongeo-upload`, `journalctl -u imazongeo-upload -f` |
| comandos | `imazongeo-upload`, `imazongeo-simulador` (em `/usr/local/bin`) |

Para atualizar, faça `git pull` e rode o script de novo (a configuração é
preservada). Não reinicie o serviço durante um processamento: ele é
perdido.

**Acesso remoto.** O serviço escuta só em `127.0.0.1:5000` e a aplicação
não tem login próprio. A forma mais simples e segura é um túnel SSH a
partir da sua máquina:

```bash
ssh -L 5000:127.0.0.1:5000 usuario@ip-da-vm   # depois abra http://127.0.0.1:5000
```

Para acesso pelo navegador sem túnel, use o nginx de exemplo em
`deploy/nginx-imazongeo-upload.conf`, que exige usuário/senha (HTTP Basic)
e pode receber HTTPS via certbot; inclua o domínio em `WEB_ALLOWED_HOSTS`.

**CLI na VM.** Os comandos podem usar a configuração do serviço, que é
legível pelo grupo `imazon`:

```bash
sudo usermod -aG imazon "$USER"      # uma vez; depois, saia e entre de novo
export IMAZON_ENV_FILE=/etc/imazongeo-upload/imazongeo-upload.env
imazongeo-upload sad --bucket imazongeo3-web --zip alertas.zip
```

O gunicorn roda com **um único worker** (`deploy/gunicorn.conf.py`), porque
os processamentos ficam em memória; não aumente `workers`.

## Interface gráfica

```bash
sudo apt install python3-tk
imazongeo-upload-gui          # ou: python -m imazongeo_upload.gui
```

No Windows, o executável é gerado com `python scripts/windows/build_exe.py`
(empacota o `.env` da raiz dentro do `.exe`) e a interface web pode ser
aberta com `scripts/windows/iniciar_web.bat`.

## Padrões de código

- Layout `src/`, metadados e dependências em `pyproject.toml` (PEP 517/621).
- Estilo PEP 8 verificado pelo [ruff](https://docs.astral.sh/ruff/)
  (pycodestyle, pyflakes, isort, pep8-naming, pyupgrade, bugbear), linhas de
  até 88 colunas e formatação automática: `make lint` / `make format`.
- Docstrings (PEP 257) e anotações de tipo (PEP 484/604) nas funções.
- Finais de linha LF (`.gitattributes`, `.editorconfig`).
- CI no GitHub Actions: lint e testes em Ubuntu 22.04 (Python 3.10) e
  24.04 (Python 3.12).
