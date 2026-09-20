/**
 * Cliente das fontes do CEMADEN.
 *
 * Dois caminhos, escolhidos conforme exista ou não `CEMADEN_TOKEN`:
 *
 *  - COM token  → API oficial PED (`sws.cemaden.gov.br/PED/rest`). Dado bruto
 *    com data-hora de cada leitura, que vira série de 10 em 10 minutos.
 *  - SEM token  → instantâneo aberto do Mapa Interativo
 *    (`resources.cemaden.gov.br/dados/311_24.json`, JSONP, sem CORS e sem
 *    credencial). Só traz o acumulado de 24 h do momento; a série é montada
 *    ciclo a ciclo, acumulando os instantâneos (ver `build.mjs`).
 *
 * Limite de uso da PED: 12 requisições/minuto para usuário externo. Toda
 * chamada passa por `throttle()`, que espaça as requisições no tempo.
 */

/** UFs da Amazônia Legal (IBGE). Uma requisição por UF em cada ciclo. */
export const UFS_AMAZONIA = ['AC', 'AM', 'AP', 'MA', 'MT', 'PA', 'RO', 'RR', 'TO'];

/** Identificador da rede observacional do CEMADEN na PED. */
export const REDE_CEMADEN = '11';

/** Estação pluviométrica. */
export const TIPO_PLUVIOMETRICA = '1';

const PED = 'https://sws.cemaden.gov.br/PED/rest';
const SGAA = 'https://sgaa.cemaden.gov.br/SGAA/rest';
const RESOURCES = 'https://resources.cemaden.gov.br';

/**
 * Espaçamento entre requisições à PED. 12 req/min = 1 a cada 5 s; usamos 6 s
 * para ter margem, o que mantém o pico — e não só a média — dentro do limite.
 */
const INTERVALO_MS = 6_000;
let ultima = 0;

const dorme = (ms) => new Promise((r) => setTimeout(r, ms));

async function throttle() {
  const espera = ultima + INTERVALO_MS - Date.now();
  if (espera > 0) await dorme(espera);
  ultima = Date.now();
}

/** Busca com tentativas: erro de rede e 5xx são temporários; 4xx, não. */
async function buscar(url, opts = {}, tentativas = 3) {
  let ultimoErro;
  for (let i = 0; i < tentativas; i++) {
    try {
      const r = await fetch(url, { ...opts, signal: AbortSignal.timeout(60_000) });
      if (r.ok) return r;
      const corpo = (await r.text()).slice(0, 300);
      if (r.status >= 400 && r.status < 500) {
        throw new Error(`HTTP ${r.status} em ${url}\n${corpo}`);
      }
      ultimoErro = new Error(`HTTP ${r.status} em ${url}\n${corpo}`);
    } catch (e) {
      if (String(e.message).includes('HTTP 4')) throw e;
      ultimoErro = e;
    }
    if (i < tentativas - 1) await dorme(2000 * 2 ** i);
  }
  throw ultimoErro;
}

// ---------------------------------------------------------------------------
// Caminho aberto (sem token)
// ---------------------------------------------------------------------------

/**
 * Instantâneo das pluviométricas: acumulado das últimas 24 h por estação.
 *
 * O arquivo é JSONP (`estacoes([...])`) apesar do `Content-Type:
 * application/json`, por isso o invólucro é removido antes do parse.
 */
export async function instantaneoAberto() {
  const r = await buscar(`${RESOURCES}/dados/311_24.json`);
  const texto = await r.text();
  const cru = texto.replace(/^\s*[A-Za-z_$][\w$]*\s*\(/, '').replace(/\)\s*;?\s*$/, '');
  const grupos = JSON.parse(cru);
  const grupo = Array.isArray(grupos) ? grupos[0] : grupos;
  if (!grupo?.estacao) throw new Error('311_24.json sem o campo `estacao`');

  // `atualizado` vem como "2026-09-20 16:04:42 UTC".
  const marca = String(grupo.atualizado ?? '').replace(' UTC', 'Z').replace(' ', 'T');
  const instante = Date.parse(marca);

  return {
    instante: Number.isFinite(instante) ? instante : Date.now(),
    estacoes: grupo.estacao
      .filter((e) => UFS_AMAZONIA.includes(e.uf))
      .map((e) => ({
        cod: String(e.codestacao),
        id: Number(e.idestacao),
        nome: String(e.nomeestacao ?? '').trim(),
        uf: String(e.uf),
        municipio: String(e.cidade ?? '').trim(),
        codibge: Number(e.codibge) || null,
        lat: Number(e.latitude),
        lon: Number(e.longitude),
        tipo: Number(e.idtipoestacao) || 1,
        // `acumulado` é a chuva das últimas 24 h em mm.
        valor: Number.isFinite(Number(e.acumulado)) ? Number(e.acumulado) : null,
      }))
      .filter((e) => Number.isFinite(e.lat) && Number.isFinite(e.lon)),
  };
}

/**
 * Série horária de 48 h de uma estação, do mesmo webservice que o Mapa
 * Interativo usa (`Access-Control-Allow-Origin: *`, sem credencial).
 *
 * Serve para PREENCHER a grade na primeira execução: sem isso, o modo aberto
 * levaria 24 h para ter história e quem abrisse o AmaView antes disso veria a
 * camada vazia. 48 h porque calcular o acumulado de 24 h na fatia mais antiga
 * exige as 24 h anteriores a ela.
 *
 * Devolve `[{ ms, mm }]` — `ms` é o INÍCIO da hora, em UTC.
 */
export async function horarias48(idEstacao) {
  const r = await buscar(`${MAPSERVICES}/horario/${idEstacao}/47`, {}, 2);
  const j = await r.json();
  const linhas = Array.isArray(j?.acumulados) ? j.acumulados : [];
  const n = Array.isArray(j?.horarios) ? j.horarios.length : 0;
  if (!n || !linhas.length) return [];

  // `acumulados` é [dia][k]: para cada k só um dia tem valor não nulo.
  const serie = new Array(n).fill(null);
  for (const dia of linhas) {
    if (!Array.isArray(dia)) continue;
    for (let k = 0; k < n; k++) {
      const v = dia[k];
      if (v !== null && v !== undefined && Number.isFinite(Number(v))) serie[k] = Number(v);
    }
  }

  // O último índice é a hora corrente; as anteriores recuam de hora em hora.
  const inicioHoraAtual = Math.floor(Date.now() / 3_600_000) * 3_600_000;
  return serie
    .map((mm, k) => ({ ms: inicioHoraAtual - (n - 1 - k) * 3_600_000, mm }))
    .filter((h) => h.mm !== null);
}

const MAPSERVICES = 'https://mapservices.cemaden.gov.br/MapaInterativoWS/resources';

/**
 * Roda `tarefa` sobre `itens` com no máximo `limite` em paralelo.
 *
 * O `mapservices` é o backend do Mapa Interativo público: seis conexões é o
 * suficiente para preencher as ~570 estações em cerca de 20 s sem pesar para
 * quem mais estiver usando o site deles.
 */
export async function emLotes(itens, tarefa, limite = 6) {
  const resultados = new Array(itens.length);
  let proximo = 0;
  async function trabalhador() {
    for (;;) {
      const i = proximo++;
      if (i >= itens.length) return;
      try {
        resultados[i] = await tarefa(itens[i], i);
      } catch {
        resultados[i] = null;
      }
    }
  }
  await Promise.all(Array.from({ length: Math.min(limite, itens.length) }, trabalhador));
  return resultados;
}

// ---------------------------------------------------------------------------
// Caminho oficial (com token)
// ---------------------------------------------------------------------------

/** Renova o JWT a partir de e-mail e senha (opcional: só se o token expirar). */
export async function renovarToken(email, senha) {
  const r = await buscar(`${SGAA}/controle-token/tokens`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password: senha }),
  });
  const j = await r.json();
  const token = j?.token ?? j?.Token ?? (Array.isArray(j) ? j[0]?.token : null);
  if (!token) throw new Error(`Resposta inesperada do /controle-token/tokens: ${JSON.stringify(j).slice(0, 200)}`);
  return token;
}

/** `aaaaMMddHHmm` em UTC, formato de data da PED. */
export function marcaPed(ms) {
  const d = new Date(ms);
  const p = (n, w = 2) => String(n).padStart(w, '0');
  return `${d.getUTCFullYear()}${p(d.getUTCMonth() + 1)}${p(d.getUTCDate())}${p(d.getUTCHours())}${p(d.getUTCMinutes())}`;
}

/**
 * Dados ambientais de uma UF num período de até 1 dia (`/pcds/dados_rede`).
 * Devolve a lista crua de leituras, normalizada.
 */
export async function dadosRede(token, uf, inicio, fim) {
  await throttle();
  const q = new URLSearchParams({
    inicio: marcaPed(inicio),
    fim: marcaPed(fim),
    uf,
    rede: REDE_CEMADEN,
    tipo_pcd: TIPO_PLUVIOMETRICA,
    formato: 'JSON',
  });
  const r = await buscar(`${PED}/pcds/dados_rede?${q}`, { headers: { token } });
  const texto = await r.text();

  let j;
  try {
    j = JSON.parse(texto);
  } catch {
    throw new Error(`Resposta não-JSON de dados_rede (${uf}): ${texto.slice(0, 200)}`);
  }
  // A PED responde com um alerta em JSON quando algo está errado (ex.: token).
  if (!Array.isArray(j)) {
    const alerta = j?.Alerta ?? j?.alerta;
    if (alerta) throw new Error(`CEMADEN recusou a chamada (${uf}): ${alerta}`);
    j = j?.dados ?? j?.leituras ?? [];
  }
  return j.map(normalizarLeitura).filter(Boolean);
}

/**
 * Uma leitura da PED → forma interna.
 *
 * Os nomes dos campos variam entre os webservices da plataforma (o catálogo
 * antigo e o novo convivem), por isso cada campo aceita vários apelidos.
 */
function normalizarLeitura(l) {
  const cod = l.codestacao ?? l.codEstacao ?? l.codibge_estacao ?? l.codigo;
  const dh = l.datahora ?? l.dataHora ?? l.datahoraUTC ?? l.data_hora ?? l.data;
  const valor = l.valor ?? l.valorMedida ?? l.chuva ?? l.acumulado ?? l.medida;
  if (!cod || !dh) return null;

  // "2026-09-20 16:04:42" (sem fuso) é UTC por convenção do CEMADEN.
  const iso = String(dh).trim().replace(' ', 'T');
  const ms = Date.parse(/[Zz]|[+-]\d{2}:?\d{2}$/.test(iso) ? iso : `${iso}Z`);
  if (!Number.isFinite(ms)) return null;

  const v = Number(valor);
  return { cod: String(cod), ms, valor: Number.isFinite(v) ? v : null };
}

/** Cadastro das PCDs de uma UF (lat/lon, nome, município). */
export async function cadastro(token, uf) {
  await throttle();
  const q = new URLSearchParams({ uf, tipoestacao: TIPO_PLUVIOMETRICA, formato: 'JSON' });
  const r = await buscar(`${PED}/pcds-cadastro/dados-cadastrais?${q}`, { headers: { token } });
  const j = await r.json();
  const lista = Array.isArray(j) ? j : (j?.dados ?? []);
  return lista
    .map((e) => ({
      cod: String(e.codestacao ?? e.codEstacao ?? ''),
      id: Number(e.idestacao ?? e.idEstacao) || null,
      nome: String(e.nomeestacao ?? e.nomeEstacao ?? '').trim(),
      uf: String(e.uf ?? uf),
      municipio: String(e.cidade ?? e.municipio ?? '').trim(),
      codibge: Number(e.codibge) || null,
      lat: Number(e.latitude),
      lon: Number(e.longitude),
      tipo: Number(e.idtipoestacao ?? e.idTipoestacao) || 1,
    }))
    .filter((e) => e.cod && Number.isFinite(e.lat) && Number.isFinite(e.lon));
}
