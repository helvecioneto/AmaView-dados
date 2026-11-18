/**
 * Funções puras dos radares da REDEMET (DECEA) no espelho do radar: a tabela
 * dos radares, a leitura da resposta da API, a extensão da imagem e quais
 * horas pedir à API. Sem rede e sem disco; testado em `redemet.test.mjs`. A
 * rede fica em `radar.mjs`.
 *
 * **Para que.** A REDEMET é a RESERVA do SIPAM (em 18/11/2026 os 11 radares
 * do SIPAM pararam de publicar às 14:10 UTC enquanto a REDEMET seguia normal)
 * e a fonte de radares que o SIPAM não publica (Rio Branco, Vilhena, Chapada
 * dos Guimarães). Produto: `maxcappi`, a refletividade máxima na coluna — o
 * mesmo que o `dbz` do SIPAM. A REDEMET não publica taxa de chuva.
 *
 * **A API.** `GET https://api-redemet.decea.mil.br/produtos/radar/maxcappi
 * ?api_key=…&data=AAAAMMDDHH&anima=15` devolve, para TODOS os radares de uma
 * vez, até 15 "quadros" de 20 em 20 min terminando na hora pedida (≈ 4 h 40)
 * — cada um com o PNG mais recente de cada radar até aquele minuto. O último
 * quadro é a varredura mais nova. O PNG fica em
 * `estatico-redemet.decea.mil.br/radar/AAAA/MM/DD/{local}/maxcappi/maps/
 * AAAA-MM-DD--HH:MM:SS.png`, com os segundos da varredura (que mudam a cada
 * uma: não dá para adivinhar o nome). Horários em UTC. Limites da chave:
 * 900/min, 10 mil/h, 100 mil/dia; o espelho usa algumas centenas por dia.
 *
 * **A imagem (medido em 18/11/2026).** PNG RGBA de 400×400 px, alfa binário,
 * numa grade AZIMUTAL EQUIDISTANTE (AEQD) centrada no radar, esfera de
 * R = 6371 km, meia largura de 400 km (2 km por pixel). Os limites
 * `lat_min…lon_max` da API são uma aproximação lat/lon dessa imagem — em
 * Manaus e Tabatinga estão errados (largura em longitude de 0,60 e 0,85 da
 * altura). Ajuste contra o SIPAM no mesmo instante (616 pares, 9 radares,
 * ecos ≥ 30 dBZ): o modelo AEQD casa 0,38–0,57 de IoU em Manaus, Tabatinga e
 * Cruzeiro do Sul, contra 0,004–0,27 da grade lat/lon nos limites da API; o
 * melhor ajuste livre ficou a ≤ 1 km de meia largura e ≤ 500 m de centro do
 * nominal. A escala de cor é contínua: 150 cores, uma a cada 0,5 dBZ de 0 a
 * 74,5 (ver `src/radar/redemet.ts` no AmaView).
 */

export const FONTE_REDEMET = 'REDEMET — Rede de Meteorologia do Comando da Aeronáutica (DECEA)';
export const FONTE_REDEMET_URL = 'https://redemet.decea.mil.br/';

export const API_REDEMET = 'https://api-redemet.decea.mil.br/produtos/radar';
export const TIPO_REDEMET = 'maxcappi';
/** Quadros por pedido (o máximo que a API dá): 15 × 20 min ≈ 4 h 40 para trás. */
export const ANIMA = 15;
/** Passo entre os blocos de horas pedidos para trás (cada pedido cobre de H−4 h a H+1 h). */
export const PASSO_BLOCO_H = 4;
/** Um bloco antigo que segue incompleto só é pedido de novo depois disto. */
export const RECONSULTA_MS = 60 * 60_000;
/** Passo dos quadros da API (s): a cobertura de um bloco é contada nesses intervalos. */
export const PASSO_QUADRO_S = 20 * 60;
/** Bloco com menos que esta fração dos intervalos de 20 min com varredura é pedido de novo. */
export const COBERTURA_MINIMA = 0.8;

/** Meia largura da imagem AEQD (m) e o raio da esfera. */
export const MEIA_LARGURA_M = 400_000;
export const RAIO_TERRA_M = 6_371_000;
export const PX_REDEMET = [400, 400];

/**
 * Os radares da REDEMET espelhados. `reservaDe`: o radar do SIPAM que ele
 * substitui quando o SIPAM não tem varredura daquele horário (o AmaView
 * decide quadro a quadro). Sem `reservaDe`: radar que só a REDEMET publica.
 *
 * `centro` [lon, lat] é o `lon_center`/`lat_center` da API em 18/11/2026 (a
 * API é relida a cada ciclo; a tabela vale quando um radar não vem nela).
 * Macapá e Tefé estavam sem imagem nas 48 h anteriores, como no SIPAM —
 * entram com a lista vazia e voltam sozinhos.
 */
export const RADARES_REDEMET = [
  { id: 'rdbe', local: 'be', nome: 'Belém', reservaDe: 'sbbe', centro: [-48.461389, -1.406667] },
  { id: 'rdbv', local: 'bv', nome: 'Boa Vista', reservaDe: 'sbbv', centro: [-60.70027777778, 2.844166667] },
  { id: 'rdch', local: 'ch', nome: 'Chapada dos Guimarães', reservaDe: null, centro: [-55.80552, -15.47093] },
  { id: 'rdcz', local: 'cz', nome: 'Cruzeiro do Sul', reservaDe: 'sbcz', centro: [-72.767778, -7.595833] },
  { id: 'rdmn', local: 'mn', nome: 'Manaus', reservaDe: 'sbmn', centro: [-59.991881, -3.149216] },
  { id: 'rdmq', local: 'mq', nome: 'Macapá', reservaDe: 'sbmq', centro: [-51.097778, -0.047222] },
  { id: 'rdpv', local: 'pv', nome: 'Porto Velho', reservaDe: 'sbpv', centro: [-63.893889, -8.715] },
  { id: 'rdrb', local: 'rb', nome: 'Rio Branco', reservaDe: null, centro: [-67.8961, -9.86163] },
  { id: 'rdsl', local: 'sl', nome: 'São Luís', reservaDe: 'sbsl', centro: [-44.2375, -2.597222] },
  { id: 'rdsn', local: 'sn', nome: 'Santarém', reservaDe: 'sbsn', centro: [-54.798889, -2.429722] },
  { id: 'rdtf', local: 'tf', nome: 'Tefé', reservaDe: 'sbtf', centro: [-64.720556, -3.377222] },
  { id: 'rdtt', local: 'tt', nome: 'Tabatinga', reservaDe: 'sbtt', centro: [-69.935, -4.248333] },
  { id: 'rdua', local: 'ua', nome: 'São Gabriel da Cachoeira', reservaDe: 'sbua', centro: [-67.056944, -0.143611] },
  { id: 'rdvh', local: 'vh', nome: 'Vilhena', reservaDe: null, centro: [-60.1037, -12.6973] },
];

/** Centro da API que se afasta da tabela mais que isto (graus) merece aviso no log. */
export const TOLERANCIA_CENTRO = 0.01;

const dois = (n) => String(n).padStart(2, '0');

/** Hora UTC (início, em ms) → `AAAAMMDDHH`, o parâmetro `data` da API. */
export function horaDaApi(ms) {
  const d = new Date(ms);
  return `${d.getUTCFullYear()}${dois(d.getUTCMonth() + 1)}${dois(d.getUTCDate())}${dois(d.getUTCHours())}`;
}

/** `"2026-11-18 20:28:08"` (UTC) → segundos epoch, ou null. */
export function tDaRedemet(texto) {
  const m = /^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/.exec(String(texto ?? '').trim());
  if (!m) return null;
  const [a, mes, d, h, mi, s] = m.slice(1).map(Number);
  const t = Date.UTC(a, mes - 1, d, h, mi, s) / 1000;
  const v = new Date(t * 1000);
  // `Date.UTC` normaliza datas impossíveis em silêncio; a volta confirma.
  if (v.getUTCFullYear() !== a || v.getUTCMonth() !== mes - 1 || v.getUTCDate() !== d || v.getUTCHours() !== h) return null;
  return t;
}

const ESTATICO = /^https:\/\/estatico-redemet\.decea\.mil\.br\/radar\/[\w/:.-]+\.png$/;

/**
 * Corpo da API → `{ porLocal: Map local → [{ t, url }] (crescente, sem
 * repetição), centros: Map local → [lon, lat] }`, ou lança erro quando não é
 * a resposta esperada (HTML de manutenção, `status: false`, formato novo).
 *
 * Só entra PNG do estático da REDEMET: o caminho vai virar download, e a API
 * é a única fonte dele.
 */
export function lerRespostaRedemet(json) {
  if (!json || json.status !== true || !json.data || !Array.isArray(json.data.radar)) {
    throw new Error(`resposta inesperada da API (${typeof json?.message === 'string' ? json.message.slice(0, 80) : 'sem data.radar'})`);
  }
  const porLocal = new Map();
  const centros = new Map();
  for (const quadro of json.data.radar) {
    if (!Array.isArray(quadro)) continue;
    for (const r of quadro) {
      const local = typeof r?.localidade === 'string' ? r.localidade.trim().toLowerCase() : null;
      if (!local) continue;
      const lon = Number(r.lon_center);
      const lat = Number(r.lat_center);
      if (Number.isFinite(lon) && Number.isFinite(lat) && !centros.has(local)) centros.set(local, [lon, lat]);
      const t = tDaRedemet(r.data);
      if (t === null || typeof r.path !== 'string' || !ESTATICO.test(r.path)) continue;
      if (!porLocal.has(local)) porLocal.set(local, new Map());
      porLocal.get(local).set(t, r.path);
    }
  }
  const saida = new Map();
  for (const [local, m] of porLocal) {
    saida.set(
      local,
      [...m.entries()].sort((a, b) => a[0] - b[0]).map(([t, url]) => ({ t, url })),
    );
  }
  return { porLocal: saida, centros };
}

/**
 * Horas a pedir à API neste ciclo (início da hora, ms), da mais nova para a
 * mais velha:
 *
 * - SEMPRE a hora corrente: traz a varredura mais nova de cada radar e as
 *   ~4 h 40 anteriores de 20 em 20 min. Com o ciclo de 5 min, a mais nova de
 *   cada ciclo dá a cadência real (~10 min) daqui para a frente;
 * - para trás, um bloco a cada 4 h até cobrir a janela, mas só o bloco em
 *   que menos de 80% dos intervalos de 20 min têm varredura da REDEMET no
 *   depósito (estreia, ou um buraco) e que não foi pedido na última hora.
 *
 * Normalmente sai uma hora só por ciclo: ~290 pedidos por dia.
 *
 * - `instantes`: as varreduras da REDEMET no depósito (s, qualquer radar);
 * - `consultas`: Map hora → quando foi pedida pela última vez (ms).
 */
export function horasAConsultar({ agoraMs, desdeS, instantes, consultas = new Map(), reconsultaMs = RECONSULTA_MS }) {
  const H = 3600_000;
  const atual = Math.floor(agoraMs / H) * H;
  const horas = [atual];
  for (let h = atual - PASSO_BLOCO_H * H; h + H > desdeS * 1000; h -= PASSO_BLOCO_H * H) {
    const ini = Math.max((h - PASSO_BLOCO_H * H) / 1000, desdeS);
    const fim = (h + H) / 1000;
    if (cobertura(instantes, ini, fim) >= COBERTURA_MINIMA) continue;
    const ultima = consultas.get(h);
    if (ultima !== undefined && agoraMs - ultima < reconsultaMs) continue;
    horas.push(h);
  }
  return horas;
}

/** Fração dos intervalos de 20 min de [ini, fim) (s) com pelo menos uma varredura em `instantes`. */
export function cobertura(instantes, ini, fim) {
  const n = Math.ceil((fim - ini) / PASSO_QUADRO_S);
  if (n <= 0) return 1;
  const cheios = new Set();
  for (const t of instantes) if (t >= ini && t < fim) cheios.add(Math.floor((t - ini) / PASSO_QUADRO_S));
  return cheios.size / n;
}

/**
 * Lon/lat de um ponto (x, y em metros) da projeção azimutal equidistante
 * esférica centrada em `centro` [lon, lat] (Snyder, 1987, eq. 20-14 e 20-15).
 */
export function aeqdInversa(centro, x, y, R = RAIO_TERRA_M) {
  const [lon0, lat0] = centro.map((v) => (v * Math.PI) / 180);
  const rho = Math.hypot(x, y);
  if (rho === 0) return [centro[0], centro[1]];
  const c = rho / R;
  const lat = Math.asin(Math.cos(c) * Math.sin(lat0) + (y * Math.sin(c) * Math.cos(lat0)) / rho);
  const lon = lon0 + Math.atan2(x * Math.sin(c), rho * Math.cos(lat0) * Math.cos(c) - y * Math.sin(lat0) * Math.sin(c));
  return [(lon * 180) / Math.PI, (lat * 180) / Math.PI];
}

/**
 * Bbox lon/lat [minx, miny, maxx, maxy] do quadrado AEQD de meia largura
 * `meia` em volta de `centro` — a borda é amostrada, porque o quadrado vira
 * um quadrilátero curvo em lon/lat. É a `extent` do manifesto (o AmaView
 * georreferencia pelo `centro` e pela `meiaLarguraM`; a extensão serve para
 * a validação e para saber se o radar está à vista).
 */
export function extentAeqd(centro, meia = MEIA_LARGURA_M) {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  const N = 64;
  for (let i = 0; i <= N; i++) {
    const s = -meia + (2 * meia * i) / N;
    for (const [x, y] of [
      [s, -meia],
      [s, meia],
      [-meia, s],
      [meia, s],
    ]) {
      const [lon, lat] = aeqdInversa(centro, x, y);
      if (lon < x0) x0 = lon;
      if (lon > x1) x1 = lon;
      if (lat < y0) y0 = lat;
      if (lat > y1) y1 = lat;
    }
  }
  const r6 = (v) => Math.round(v * 1e6) / 1e6;
  return [r6(x0), r6(y0), r6(x1), r6(y1)];
}

/** Centro a publicar: o da API quando válido (com aviso se divergir da tabela), senão o da tabela. */
export function escolherCentro(radar, daApi) {
  const ok =
    Array.isArray(daApi) &&
    daApi.length === 2 &&
    daApi.every(Number.isFinite) &&
    daApi[0] >= -80 &&
    daApi[0] <= -30 &&
    daApi[1] >= -35 &&
    daApi[1] <= 10;
  if (!ok) return { centro: [...radar.centro], fonte: 'tabela', divergencia: null };
  const divergencia = Math.max(Math.abs(daApi[0] - radar.centro[0]), Math.abs(daApi[1] - radar.centro[1]));
  return { centro: [...daApi], fonte: 'api', divergencia };
}
