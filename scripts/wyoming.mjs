/**
 * Cliente das sondagens da Universidade de Wyoming.
 *
 * O endpoint clássico (`/cgi-bin/sounding`) foi descontinuado e responde 404.
 * O novo, extraído do `onSubmit()` do formulário deles:
 *
 *   /wsgi/sounding?datetime=YYYY-MM-DD HH:00:00&id={WMO}&src=UNKNOWN&type=TEXT:LIST
 *
 * `src=UNKNOWN` é o padrão do próprio formulário; `id` aceita o número WMO ou
 * o código ICAO. Data sem sondagem devolve 400 com texto explicando — isso é
 * ausência de dado, não erro de parâmetro, e os dois não podem se confundir.
 *
 * Medido: a sondagem das 12Z fica disponível em ~7 h, contra ~2 dias do IGRA.
 * É por isso que a fonte do que está no ar é esta.
 */

const BASE = 'https://weather.uwyo.edu/wsgi/sounding';

/** Servidor acadêmico, sem SLA: uma conexão de cada vez e com folga. */
const INTERVALO_MS = 700;
let ultima = 0;

const dorme = (ms) => new Promise((r) => setTimeout(r, ms));

async function espacar() {
  const falta = ultima + INTERVALO_MS - Date.now();
  if (falta > 0) await dorme(falta);
  ultima = Date.now();
}

/** `YYYY-MM-DD HH:00:00` em UTC — o único formato que o servidor aceita. */
export function marcaWyoming(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} ${p(d.getUTCHours())}:00:00`;
}

/** Horários de lançamento cobertos (UTC). O balão sobe duas vezes por dia. */
export const HORARIOS = [0, 12];

/**
 * Instantes de sondagem dentro de uma janela, do mais recente para o mais
 * antigo. `fim` normalmente é agora.
 */
export function instantesDeSondagem(fim, horas = 48) {
  const out = [];
  const d = new Date(fim);
  d.setUTCMinutes(0, 0, 0);
  for (let h = 0; h <= horas; h++) {
    const t = d.getTime() - h * 3_600_000;
    if (HORARIOS.includes(new Date(t).getUTCHours())) out.push(t);
  }
  return out;
}

/**
 * Uma sondagem. `null` quando aquele horário não tem lançamento — ausência
 * esperada, não falha.
 */
export async function sondagem(idWmo, ms) {
  await espacar();
  const q = new URLSearchParams({
    datetime: marcaWyoming(ms),
    id: String(idWmo),
    src: 'UNKNOWN',
    type: 'TEXT:LIST',
  });

  let r;
  try {
    r = await fetch(`${BASE}?${q}`, { signal: AbortSignal.timeout(45_000) });
  } catch (e) {
    throw new Error(`rede: ${e.message}`);
  }

  // 400/404 com corpo curto = não há sondagem nesse horário.
  if (r.status === 400 || r.status === 404) {
    void r.text().catch(() => {});
    return null;
  }
  if (!r.ok) throw new Error(`HTTP ${r.status}`);

  return lerPerfil(await r.text(), ms);
}

/**
 * Extrai o perfil do HTML.
 *
 * Parsear HTML é frágil — a página pode mudar de forma sem aviso. Por isso a
 * função devolve `null` só quando o bloco não existe (sem sondagem) e LANÇA
 * quando o bloco existe mas não dá para ler: uma mudança de formato tem que
 * aparecer como erro no manifesto, nunca como silêncio.
 */
export function lerPerfil(html, ms) {
  const bloco = /<PRE>([\s\S]*?)<\/PRE>/i.exec(html);
  if (!bloco) return null;

  const linhas = bloco[1].split('\n');
  const iCab = linhas.findIndex((l) => /\bPRES\b/.test(l) && /\bHGHT\b/.test(l));
  if (iCab < 0) throw new Error('bloco <PRE> sem cabeçalho PRES/HGHT — formato mudou?');

  const niveis = [];
  for (let i = iCab + 3; i < linhas.length; i++) {
    const campos = linhas[i].trim().split(/\s+/);
    if (campos.length < 3) continue;
    const [pres, hght, temp, dwpt, relh] = campos.map(Number);
    if (!Number.isFinite(pres) || !Number.isFinite(hght)) continue;
    niveis.push({
      p: pres,
      z: hght,
      t: Number.isFinite(temp) ? temp : null,
      d: Number.isFinite(dwpt) ? dwpt : null,
      u: Number.isFinite(relh) ? relh : null,
    });
  }
  if (niveis.length === 0) throw new Error('bloco <PRE> sem nenhum nível legível');

  return { ms, niveis, indices: lerIndices(html) };
}

/**
 * Índices termodinâmicos, que a própria página traz numa tabela.
 *
 * Aproveitá-los evita reimplementar termodinâmica — e errar nela em silêncio.
 */
export function lerIndices(html) {
  const out = {};
  // Linhas no formato "SIGLA ... Descrição ... valor ... unidade" em <td>.
  const texto = html.replace(/<[^>]+>/g, '\n');
  const linhas = texto.split('\n').map((l) => l.trim()).filter(Boolean);
  const querido = new Set(['PWAT', 'MUCAPE', 'MUCIN', 'LCLP', 'LCLT', 'KINX', 'TOTL']);
  for (let i = 0; i < linhas.length; i++) {
    if (!querido.has(linhas[i])) continue;
    // O valor é o primeiro número nas linhas seguintes.
    for (let k = i + 1; k < Math.min(i + 5, linhas.length); k++) {
      const v = Number(linhas[k]);
      if (Number.isFinite(v)) {
        out[linhas[i]] = v;
        break;
      }
    }
  }
  return out;
}

/**
 * Trajetória do balão: o CSV traz `time`, `latitude` e `longitude` a cada
 * segundo, que é a posição medida por GPS enquanto a sonda sobe.
 *
 * `null` quando a estação publica só os níveis obrigatórios — nesses casos o
 * CSV vem com a mesma posição e o mesmo instante em todas as linhas, e desenhar
 * uma trilha de 0 km seria inventar um percurso que ninguém mediu.
 */
export async function trajetoria(idWmo, ms) {
  await espacar();
  const q = new URLSearchParams({
    datetime: marcaWyoming(ms),
    id: String(idWmo),
    src: 'UNKNOWN',
    type: 'TEXT:CSV',
  });

  let r;
  try {
    r = await fetch(`${BASE}?${q}`, { signal: AbortSignal.timeout(60_000) });
  } catch (e) {
    throw new Error(`rede (csv): ${e.message}`);
  }
  if (r.status === 400 || r.status === 404) {
    void r.text().catch(() => {});
    return null;
  }
  if (!r.ok) throw new Error(`HTTP ${r.status} (csv)`);

  return lerTrajetoria(await r.text());
}

/** Colunas do CSV do Wyoming que nos interessam. */
const COLS = {
  time: 'time',
  lon: 'longitude',
  lat: 'latitude',
  z: 'geopotential height_m',
};

export function lerTrajetoria(texto) {
  const pre = /<PRE>([\s\S]*?)<\/PRE>/i.exec(texto);
  const corpo = (pre ? pre[1] : texto).trim();
  const linhas = corpo.split(/\r?\n/).filter((l) => l.includes(','));
  if (linhas.length < 3) return null;

  const cab = linhas[0].split(',').map((c) => c.trim());
  const iT = cab.indexOf(COLS.time);
  const iLon = cab.indexOf(COLS.lon);
  const iLat = cab.indexOf(COLS.lat);
  const iZ = cab.indexOf(COLS.z);
  if (iT < 0 || iLon < 0 || iLat < 0 || iZ < 0) return null;

  const pts = [];
  for (let i = 1; i < linhas.length; i++) {
    const c = linhas[i].split(',');
    const lon = Number(c[iLon]);
    const lat = Number(c[iLat]);
    const z = Number(c[iZ]);
    const t = Date.parse(String(c[iT]).trim().replace(' ', 'T') + 'Z');
    if (!Number.isFinite(lon) || !Number.isFinite(lat) || !Number.isFinite(z)) continue;
    pts.push({ lon, lat, z, ms: Number.isFinite(t) ? t : null });
  }
  if (pts.length < 3) return null;

  // Estação que só publica níveis obrigatórios repete a posição: não há
  // percurso medido, e desenhar um seria invenção.
  const lons = pts.map((p) => p.lon);
  const lats = pts.map((p) => p.lat);
  const espalhamento = Math.max(...lons) - Math.min(...lons) + (Math.max(...lats) - Math.min(...lats));
  if (espalhamento < 0.01) return null;

  return reduzirTrilha(pts);
}

/**
 * Reduz a trilha a poucas dezenas de pontos, espaçados por ALTURA.
 *
 * Belém veio com 4.033 posições, uma por segundo. Espaçar por altura, e não
 * por tempo, preserva a forma do percurso: o balão sobe devagar no começo e
 * rápido no fim, então amostrar por tempo concentraria quase todos os pontos
 * na base.
 */
export function reduzirTrilha(pts, alvo = 60) {
  if (pts.length <= alvo) return pts;
  const zMin = Math.min(...pts.map((p) => p.z));
  const zMax = Math.max(...pts.map((p) => p.z));
  if (!(zMax > zMin)) return pts.slice(0, alvo);

  const passo = (zMax - zMin) / (alvo - 1);
  const out = [pts[0]];
  let proximo = zMin + passo;
  for (const p of pts) {
    if (p.z >= proximo) {
      out.push(p);
      while (proximo <= p.z) proximo += passo;
    }
  }
  const ultimo = pts[pts.length - 1];
  if (out[out.length - 1] !== ultimo) out.push(ultimo);
  return out;
}

/** Página do Skew-T no Wyoming, para quem quiser o diagrama de verdade. */
export function urlSkewT(idWmo, ms) {
  const q = new URLSearchParams({
    datetime: marcaWyoming(ms),
    id: String(idWmo),
    src: 'UNKNOWN',
    type: 'PNG:SKEWT',
  });
  return `${BASE}?${q}`;
}
