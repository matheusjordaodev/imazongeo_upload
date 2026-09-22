# ImazonGeo Upload

Publicação dos datasets do ImazonGeo no AWS S3, para download e para os
dashboards:

| Dataset | Frequência | Entrada |
|---|---|---|
| **SAD** | mensal | ZIP recebido com os alertas acumulados (um arquivo por tipo + camada) |
| **Floreser** | anual | arquivo do ano, gravado no banco de dados |
| **Ameaça & Pressão** | trimestral | arquivo do trimestre, gravado no banco de dados |
| **SIMEX** | anual | arquivo do ano com todas as camadas, gravado no banco de dados |

Floreser, Ameaça & Pressão e SIMEX têm o banco PostgreSQL/PostGIS como fonte:
o envio atualiza o banco e os arquivos do S3 são gerados a partir dele (veja
[Banco de dados](#banco-de-dados-sad-simex-ameaça--pressão-e-floreser)).

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
  banco/             banco PostGIS: padrão, validação, gravação, exportação e comando imazongeo-banco
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
| `DATABASE_URL` | banco PostgreSQL/PostGIS de SIMEX, Ameaça & Pressão e Floreser (ex.: `postgresql://usuario:senha@localhost:5432/imazongeo`) |

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

Floreser, SIMEX e Ameaça & Pressão passaram a ser publicados pelo banco
(`imazongeo-banco`, abaixo). O comando `imazongeo-upload` para essas bases é o
fluxo legado: publica os nomes antigos sem passar pelo banco. Ele procura os
arquivos em `--base-dir` (padrão `dados/`):

```text
dados/
  floreser/2025/{shapefile,csv,geojson}/floreser_2025.{zip,csv,geojson}
  simex/2025/{shapefile,csv,geojson}/simex_unificado_2025.{zip,csv,geojson}
  ameaca_pressao/2025/T3/{shapefile,csv,geojson}/ameaca_e_pressao_3_trimestre_2025.{zip,csv,geojson}
```

## Banco de dados (SAD, SIMEX, Ameaça & Pressão e Floreser)

O banco é a fonte dos dados. Cada envio passa por estas etapas:

1. **Validação e padronização** (`banco/entrada.py`, `banco/normalizacao.py`).
   O envio é um ZIP com GeoJSONs ou Shapefiles, ou um único GeoJSON; o Floreser
   aceita também CSV. Ele deve trazer todas as camadas do período: as 6 do SIMEX
   ou os 8 rankings (recorte × classe) do A&P. Os nomes de campo de todas as
   versões publicadas desde 2007 são convertidos para o padrão.
2. **Correção automática** (`banco/correcao.py`). O log do envio informa quantas
   correções de cada tipo foram feitas:
   - **Geometrias inválidas:** corrigidas com `make_valid`, sem alterar a área.
     O caso do histórico são 175 polígonos do SIMEX com anel que toca a si
     mesmo. Linhas ou pontos sem área gerados pelo reparo são descartados.
   - **Textos com codificação quebrada:** consertados, ex.:
     `CARAJÃ\x83Â\x81S` → `CARAJÁS`. Espaços sobrando são removidos.
   - **Valores de vocabulário conhecido:** grafias padronizadas, ex.:
     uso e categoria do A&P, meses da legenda (`janeiro a marco` → `janeiro a
     março`) e UFs.
   - **Nomes do A&P com letra perdida (`�`):** substituídos pelo nome já gravado
     no banco que corresponde, ex.: `APA Arquip�lago do Maraj�` → `APA
     Arquipélago do Marajó`.
   - **`area_ha` do SIMEX:** recalculado pela geometria (área geodésica). No
     arquivo de origem, as camadas fundiárias trazem a área do polígono antes
     do recorte. O valor original fica guardado em `atributos`.
   - **Registros idênticos no mesmo envio (mesmo ano ou trimestre):** removidos.
     Repetições em anos diferentes e em camadas diferentes do SIMEX são mantidas.

   Continuam sendo erro, porque não há como decidir sozinho: camada ou ranking
   faltando, registro de outro ano, a mesma área no mesmo ranking com dados
   diferentes e, no Floreser, a mesma UF/município/idade com áreas diferentes.
   Os atributos originais de cada registro ficam na coluna `atributos` do banco.
3. **Gravação**: o envio substitui o período no banco (tabela `carga`), numa
   única transação.
4. **Exportação e publicação automática**: GeoJSON, CSV e Shapefile são gerados
   a partir do banco e enviados ao S3. O registro de cada envio fica na tabela
   `publicacao`.

O padrão (`banco/padrao.py`) define campos, tipos e nomes de arquivo. Os
nomes de campo são iguais nas tabelas e nos três formatos e têm até 10
caracteres, o limite do Shapefile:

| Dataset | Campos | Arquivos no S3 |
|---|---|---|
| SIMEX | `ano camada categoria uf municipio cod_mun territorio subclasse area_ha` + geometria | `simex/{geojson,csv,shapefile}/simex_AAAA.*` |
| Ameaça & Pressão | `ano trimestre legenda recorte classe posicao celulas nome modalidade categoria uso jurisdicao estado` + geometria | `ameaca_e_pressao/{...}/ameaca_e_pressao_AAAA_tN.*` |
| Floreser | `ano cod_uf estado cod_mun municipio idade area_ha` (sem geometria) | `floreser/csv/floreser_AAAA.csv` |

- **GeoJSON:** WGS 84 com precisão total.
- **CSV:** UTF-8 com BOM, para abrir com acentos corretos no Excel.
- **Shapefile:** ZIP com `.cpg` em UTF-8.

Os arquivos legados (`simex_unificado_AAAA`, `ameaca_e_pressao_T_trimestre_AAAA`,
`floreser/floreser_AAAA.csv` e `dashboard/`) não são alterados. Os dashboards
passarão a ler o banco: as visões `imazongeo.vw_simex`, `vw_ameaca_pressao` e
`vw_floreser` já entregam os campos do padrão.

Criação do banco (uma vez, PostgreSQL 15+ com PostGIS 3):

```bash
sudo -u postgres createdb -O usuario imazongeo
sudo -u postgres psql -d imazongeo -c "CREATE EXTENSION postgis"
# PostgreSQL 16 do Ubuntu 24.04: o JIT derruba consultas pesadas com PostGIS
sudo -u postgres psql -d imazongeo -c "ALTER DATABASE imazongeo SET jit = off"
```

Comandos (`python -m imazongeo_upload.banco` equivale a `imazongeo-banco`):

```bash
imazongeo-banco criar-schema                   # todos os datasets
imazongeo-banco criar-schema --dataset sad     # só o SAD (e as tabelas de controle)
# Envio → banco → S3 (modo prévia por padrão; --modo simulation | real)
imazongeo-banco enviar simex_2025.zip --dataset simex --ano 2025 --modo real --public
imazongeo-banco enviar ap.zip --dataset ameaca_pressao --ano 2025 --trimestre 3 --modo real
# Histórico: baixa os arquivos legados do S3 público (~1,6 GB em dados/espelho_s3/),
# grava no banco e publica no padrão novo
imazongeo-banco baixar-legado
imazongeo-banco importar-legado
imazongeo-banco republicar --dataset simex --modo real --public
```

Os modos são os mesmos da aplicação:

- **Prévia:** só valida, sem abrir o banco nem o S3.
- **Simulação:** grava numa transação desfeita ao final e envia para um S3 local.
- **Real:** grava no banco e publica. Se a publicação falhar depois da
  gravação, o comando `republicar` gera e envia de novo.

A importação do legado nunca sobrescreve um período enviado pela aplicação.

Os testes de integração (`tests/test_banco_integracao.py` e
`tests/test_banco_sad.py`) rodam com `BANCO_TEST_DATABASE_URL` apontando para um
banco descartável com PostGIS; eles apagam o esquema `imazongeo` desse banco.

### SAD no banco

O envio do SAD (web, `imazongeo-upload sad` e interface gráfica) grava os
alertas no banco antes de publicar no S3. A gravação acontece quando
`DATABASE_URL` está configurada; sem ela, o SAD vai só para o S3, com um aviso.
Se a gravação falhar, nada é enviado ao S3. Os arquivos do S3 (ZIPs mensais e
CSVs do dashboard) continuam sendo gerados como antes.

- **Tabela:** `imazongeo.sad_alerta`, com um alerta por linha e geometria.
- **Visão:** `imazongeo.vw_sad`, lida pelo dashboard do SAD. Campos: `ano mes
  tipo camada sensor uf municipio territorio uso jurisdicao area_km2`.
- **Tipo e camada:** vêm do nome de cada arquivo, ex.:
  `alertas_sad_desmatamento_01_2008_07_2026_municipios.geojson`.
- **Substituição:** cada mês de cada tipo/camada é uma carga (coluna
  `particao` da tabela `carga`). Um envio substitui, só para o tipo/camada do
  arquivo, todos os meses do intervalo do nome, inclusive meses que ficaram sem
  alertas. As outras partes não mudam.
- **Correções:** as mesmas do padrão (textos, grafias, geometrias, repetidos).
  A `area_km2` oficial do arquivo é mantida; só é recalculada pela geometria se
  vier vazia ou diferir mais de 1% da área do polígono.
- **Arquivos grandes:** os de mais de 1 GB são lidos em blocos de 20 mil alertas.

Para gravar arquivos já recebidos, sem publicar no S3:

```bash
imazongeo-banco importar-sad dados/sad/*.geojson            # prévia
imazongeo-banco importar-sad dados/sad/*.geojson --modo real
imazongeo-banco importar-sad parte1.zip parte2.zip --modo real
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
Para SIMEX, Ameaça & Pressão e Floreser, o envio grava no banco
(`DATABASE_URL`) e publica no S3 os arquivos gerados a partir dele.

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
