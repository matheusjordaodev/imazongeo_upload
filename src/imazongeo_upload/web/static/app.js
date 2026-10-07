const form = document.querySelector('form');
// Endereço da API: vem da página, para funcionar sob um caminho (ex.: /upload/).
// Sem o dado (página antiga em cache), vale o caminho da própria página — nunca
// a raiz do domínio, onde costuma responder outro serviço.
const API = form.dataset.api || new URL('api/jobs', location.href).pathname;
const field = name => form.elements.namedItem(name);
const show = (id, visible) => document.getElementById(id).hidden = !visible;
const now = new Date();
field('year').value = now.getFullYear();
field('month').value = now.getMonth() + 1;
field('quarter').value = Math.floor(now.getMonth() / 3) + 1;
field('mode').value = 'real';
// Bases gravadas no banco: envio → banco → GeoJSON, CSV e Shapefile no S3
const BANCO = {
  simex: {raiz: 'simex', periodo: 'ano', guia: 'Envie o ZIP do ano com todas as camadas do SIMEX (municípios, imóveis rurais, assentamentos, terras indígenas, unidades de conservação e terras não destinadas), em GeoJSON ou Shapefile, um arquivo por camada ou um arquivo com o campo de camada.'},
  ameaca_pressao: {raiz: 'ameaca_e_pressao', periodo: 'trimestre', guia: 'Envie o ZIP ou GeoJSON do trimestre com os 8 rankings (geral e por categoria, ameaça e pressão). Arquivos com vários trimestres são filtrados pelo ano e trimestre selecionados.'},
  floreser: {raiz: 'floreser', periodo: 'ano', guia: 'Envie o arquivo do ano (ZIP com GeoJSON/Shapefile, GeoJSON ou CSV) com área por município, UF e idade.'},
};
function update() {
  const dataset = field('dataset').value;
  const sad = dataset === 'sad';
  const banco = BANCO[dataset];
  show('operation-field', false); show('format-field', false);
  show('month-field', sad); show('quarter-field', dataset === 'ameaca_pressao'); show('sad-options', sad);
  show('sad-classification', sad);
  field('files').accept = sad ? '.zip' : '.zip,.geojson' + (dataset === 'floreser' ? ',.csv' : '');
  field('files').multiple = sad;
  document.getElementById('file-label').textContent = sad ? 'Selecione o ZIP com GeoJSON, GeoPackage ou Shapefile' : 'Selecione o ZIP (GeoJSON ou Shapefile) ou o GeoJSON' + (dataset === 'floreser' ? ' ou CSV' : '');
  const mode = field('mode').value;
  if (sad) document.getElementById('guide').textContent = 'Envie um ZIP com as camadas em GeoJSON, GeoPackage (.gpkg) ou Shapefile, com qualquer nome. Para nomes livres, selecione o tipo de alerta e a camada acima; arquivos com nomes já reconhecidos continuam sendo identificados automaticamente. Os períodos são lidos dos campos ANO e MES. Envie um GeoJSON por tipo e camada; para nomes livres de camadas diferentes, faça envios separados. GeoJSON, CSV e Shapefile são gerados automaticamente.';
  else {
    const periodo = banco.periodo === 'trimestre' ? 'AAAA_tN' : 'AAAA';
    const formatos = dataset === 'floreser' ? 'csv' : '{geojson,csv,shapefile}';
    document.getElementById('guide').textContent = banco.guia + ` O envio substitui o período no banco de dados e, em seguida, os arquivos são gerados a partir do banco no padrão ImazonGeo e publicados em ${banco.raiz}/${formatos}/${banco.raiz}_${periodo}.`;
  }
  show('real-options', mode === 'real');
  field('password').required = mode === 'real'; field('confirm').required = mode === 'real';
  document.getElementById('run').textContent = ({dry_run:'Executar prévia →', simulation:'Executar simulação →', real:'Enviar para o S3 →'})[mode];
  document.getElementById('mode-help').textContent = (banco ? {dry_run:'A prévia valida o arquivo e mostra o que seria gravado no banco e publicado, sem acessar banco nem AWS.', simulation:'Grava no banco numa transação desfeita ao final e publica num S3 temporário local: nada muda no banco nem na AWS.', real:'Grava no banco de dados e publica os arquivos gerados na AWS, com as credenciais do .env do servidor.'} : {dry_run:'A prévia mostra as operações previstas, sem enviar dados à AWS.', simulation:'Processa os arquivos usando um S3 temporário local, sem credenciais AWS. Os arquivos simulados são apagados ao concluir.', real:'Envia os dados para a AWS usando as credenciais configuradas no .env do servidor.'})[mode];
}
form.addEventListener('change', update);
field('files').addEventListener('change', () => {
  document.getElementById('selection').textContent = Array.from(field('files').files, f => `${f.name} (${(f.size/1024/1024).toFixed(1)} MB)`).join(' · ') || 'Nenhum arquivo selecionado.';
});
update();
async function jsonResponse(response, campo) {
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error((data && data.error) || `Falha na operação (HTTP ${response.status}).`);
  // Resposta 200 fora do formato esperado (ex.: página de outro serviço no caminho)
  if (!data || (campo && data[campo] === undefined)) throw new Error((data && data.error) || 'O servidor retornou uma resposta inesperada. Recarregue a página (Ctrl+F5) e tente de novo.');
  return data;
}
form.addEventListener('submit', async event => {
  event.preventDefault();
  const error = document.getElementById('error');
  const status = document.getElementById('status');
  const logs = document.getElementById('logs');
  error.textContent = '';
  const body = new FormData(form);
  const controls = Array.from(form.querySelectorAll('input,select,button'));
  controls.forEach(el => el.disabled = true);
  status.textContent = 'Enviando arquivos ao servidor…';
  logs.textContent = '';
  try {
    const job = await jsonResponse(await fetch(API, {method:'POST', body, headers:{'X-CSRF-Token':document.querySelector('meta[name="csrf-token"]').content}}), 'id');
    status.textContent = 'Processando…';
    let failures = 0;
    while (true) {
      let data;
      try { data = await jsonResponse(await fetch(API + '/' + job.id), 'logs'); failures = 0; }
      catch (err) { if (++failures >= 5) throw new Error('Conexão perdida. O processamento pode continuar no servidor; não repita o envio sem verificar.'); await new Promise(r => setTimeout(r, 2000)); continue; }
      logs.textContent = data.logs.join('\n'); logs.scrollTop = logs.scrollHeight;
      if (data.status !== 'running') {
        status.textContent = data.status === 'done' ? 'Concluído' : 'Falha no processamento';
        if (data.status === 'error') error.textContent = 'Confira o erro no acompanhamento abaixo.';
        break;
      }
      await new Promise(resolve => setTimeout(resolve, 1000));
    }
  } catch (err) { error.textContent = err.message; status.textContent = 'Não concluído'; }
  finally { controls.forEach(el => el.disabled = false); field('password').value = ''; field('confirm').checked = false; update(); }
});
