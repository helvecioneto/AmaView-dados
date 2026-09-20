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
