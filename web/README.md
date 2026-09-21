# Imazon Upload — versão web local

O diretório S3 de Ameaça & Pressão é `ameaca_e_pressao`: downloads usam `ameaca_e_pressao/{geojson,csv,shapefile}/` e o dashboard usa `dashboard/ameaca_e_pressao/{geojson,csv,shapefile}/`. A mudança vale para novos envios; arquivos anteriormente enviados para outro diretório não são movidos automaticamente.

Interface no navegador que reutiliza `main.py` e `simulador.py` da aplicação desktop. Execute a partir da raiz do projeto com Python 3.10 ou superior:

```powershell
python -m pip install -r web/requirements.txt
python web/server.py
```

Abra http://127.0.0.1:5000. Após instalar as dependências, também é possível executar `web/iniciar.bat`.

## Funcionalidades

- SAD: ZIP obrigatório contendo GeoJSONs (um por tipo/camada). Converte automaticamente para Shapefile e CSV e publica os três formatos em `sad/shapefile/sad_AAAA_MM.zip`, `sad/csv/sad_AAAA_MM.zip` e `sad/geojson/sad_AAAA_MM.zip`, além dos CSVs do dashboard. A prévia lista as operações; a simulação e o upload real executam as conversões. Aceita vários ZIPs, período mensal e downloads de um mês ou de todos.
- Floreser e SIMEX: ZIP com GeoJSONs anuais para download ou dashboard. Todos os GeoJSONs do ZIP são reunidos em uma base para o período informado.
- Ameaça & Pressão: seleção de ano e trimestre tanto para download quanto para dashboard por categoria. No dashboard, cada categoria mantém seu arquivo e nome próprios.
- Todas as bases exigem ZIP contendo GeoJSONs e geram automaticamente GeoJSON, CSV e Shapefile. Não é necessário escolher o formato. O modo inicial da interface é upload real na AWS, com senha e confirmação; prévia e simulação continuam disponíveis para testes.
- Prévia sem envio, simulação completa em S3 local temporário e upload real com senha e confirmação.
- Seleção de arquivos, organização automática dos nomes, acompanhamento de logs e indicação de falhas.

Para upload real, configure `ACCESS_KEY`, `PRIVATE_KEY`, `AWS_REGION` e `UPLOAD_PASSWORD` no `.env` da raiz, como na aplicação desktop. As credenciais não são enviadas ao navegador. O upload real pode substituir dados existentes conforme as regras do processamento original.

O servidor aceita somente acesso local na porta 5000 e executa uma tarefa por vez. Não é uma implantação pública: acesso remoto exige autenticação, HTTPS e um servidor de produção. Limite de entrada: 1 GB por requisição. Não há limite de tamanho descompactado por ZIP na aplicação. Entradas e resultados simulados são temporários e removidos após cada execução; a simulação começa sem histórico S3. Os últimos 20 processamentos ficam em memória enquanto o servidor está aberto. Não feche o servidor durante um processamento.

Todas as bases recebem exclusivamente ZIP com arquivos `.geojson` em FeatureCollections não vazias. ZIPs sem GeoJSON ou com Shapefile/CSV são recusados. As geometrias devem ser válidas e compatíveis com Shapefile. Os arquivos recebidos devem corresponder ao período informado; nas bases não SAD esse período define o nome da saída, sem filtrar as feições.

Na conversão de SIMEX, Floreser e Ameaça & Pressão, geometrias inválidas são reparadas automaticamente com `make_valid` antes da geração dos formatos. O log informa a quantidade corrigida por arquivo. Nenhum registro é descartado: geometrias ausentes, vazias ou reparos que produzam componentes não poligonais interrompem a operação com uma mensagem específica. Os arquivos de entrada permanecem intactos; os reparos são aplicados às saídas. Após atualizar o código, reinicie `python web/server.py` para carregar a correção.

Em Ameaça & Pressão, os atributos `ANO` e `TRIMESTRE` (ou `MES`, convertido em trimestre) filtram o período selecionado. Quando esses campos não existem, o arquivo enviado é considerado do período informado na tela, e recebe `ANO` e `TRIMESTRE`. No dashboard, o GeoJSON existente tem todos os registros desse trimestre/ano substituídos, mantendo os demais períodos; os três formatos são gerados a partir desse resultado. Histórico sem informação suficiente de período bloqueia a atualização para evitar substituição indevida. Use os mesmos nomes de categoria para atualizar o mesmo objeto no S3.

Para SAD, os GeoJSONs dentro do ZIP podem ter qualquer nome. Para nomes livres, selecione na tela o tipo de alerta e a camada de destino. Nomes já reconhecidos continuam com identificação automática. Envie um GeoJSON por tipo/camada; se houver nomes livres de camadas diferentes, faça envios separados com a classificação correspondente. O servidor organiza os nomes de saída internamente, usando os períodos presentes nos atributos. Continuam obrigatórios polígonos Polygon/MultiPolygon e propriedades `ANO` e `MES` inteiras. Informe um período presente nos dados ou selecione todos os meses.

A opção **Gerar Download para diretório espacial** processa todos os pares ANO/MES presentes nos GeoJSONs. Para cada mês, gera um ZIP com Shapefiles (incluindo seus componentes), um ZIP com CSVs de atributos e um ZIP com GeoJSONs, com um arquivo por tipo/camada. No upload real, publica `sad/shapefile/sad_AAAA_MM.zip`, `sad/csv/sad_AAAA_MM.zip` e `sad/geojson/sad_AAAA_MM.zip`. Desmarcada, processa somente o mês/ano selecionado. A opção de dashboard é independente; prévia e simulação não enviam à AWS.

Nas demais bases, GeoJSON e CSV são enviados diretamente às pastas `{base}/geojson/` e `{base}/csv/`. O Shapefile é compactado com seus componentes em `{base}/shapefile/`. A operação dashboard usa o prefixo `dashboard/{base}/` nos três formatos e gera CSV/Shapefile a partir do GeoJSON mesclado com o histórico existente, usando as regras de MES/ANO da aplicação original. Todas as conversões dessas bases terminam antes do primeiro upload. Nomes de campos do Shapefile ficam sujeitos ao limite de 10 caracteres do formato.

## Verificação

```powershell
python -m unittest discover -s web/tests -v
```
