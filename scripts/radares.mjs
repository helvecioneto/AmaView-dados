/**
 * Funções puras do espelho dos radares do SIPAM: a tabela dos radares, a
 * leitura da listagem do Apache, o nome ↔ instante dos arquivos, a janela de
 * 48 h, a conferência do PNG, a extensão vinda da WFS, o planejamento do que
 * baixar e a montagem do manifesto.
 *
 * Sem rede e sem disco, testado em `radar.test.mjs`. A rede e os arquivos
 * ficam em `radar.mjs`.
 */

export const FONTE = 'SIPAM — Sistema de Proteção da Amazônia (Censipam)';
export const FONTE_URL = 'https://siger.sipam.gov.br/radar/';

/**
 * Produtos espelhados: `dbz` é a refletividade, `rate` a taxa de chuva em
 * mm/h. O SIPAM publica também o `vil` (água líquida integrada na vertical),
 * que fica de fora: o AmaView não o exibe, e cada produto a mais soma umas
 * 2.300 imagens às 48 h publicadas.
 */
export const PRODUTOS = ['dbz', 'rate'];

/**
 * Os radares, com a extensão de reserva.
 *
 * `extent` é [minx, miny, maxx, maxy] em EPSG:4326 e marca as BORDAS
 * EXTERNAS do PNG, não o centro dos pixels das pontas: é o que um
 * `ImageStatic` do OpenLayers espera. A autoridade é a WFS de cobertura do
 * SIPAM, relida a cada ciclo (`escolherExtent`); esta tabela é a cópia dela
 * em 21/09/2026, para o dia em que a WFS não responder.
 *
 * Medido em 21/09/2026, para quem for mexer na animação:
 * - cadência de 10 min em sbbe, sbsl, sbsn e sbtt; 12 min em sbcz e sbmn
 *   (Manaus ainda pula o :00 de cada hora); ~10–11 min com deriva em sbbv,
 *   sbpv e sbua;
 * - latência de 10 a 21 min entre a varredura e o arquivo aparecer;
 * - PNG RGBA de 954×954 (sbbe: 1000×1000), de 4 a 40 KB;
 * - sbmq e sbtf estavam com o diretório vazio — entram no manifesto com as
 *   listas vazias, e voltam sozinhos quando o SIPAM religar.
 */
export const RADARES = [
  { id: 'sbbe', nome: 'Belém', extent: [-50.6378, -3.5827, -46.2858, 0.7693] },
  { id: 'sbbv', nome: 'Boa Vista', extent: [-62.8762, 0.66821, -58.5242, 5.02021] },
  { id: 'sbcz', nome: 'Cruzeiro do Sul', extent: [-74.9457, -9.77153, -70.5937, -5.41953] },
  { id: 'sbmn', nome: 'Manaus', extent: [-62.1674, -5.32489, -57.8154, -0.97289] },
  { id: 'sbmq', nome: 'Macapá', extent: [-53.2729, -2.2216, -48.9209, 2.1304] },
  { id: 'sbpv', nome: 'Porto Velho', extent: [-66.0598, -10.89114, -61.7078, -6.53914] },
  { id: 'sbsl', nome: 'São Luís', extent: [-46.4153, -4.77648, -42.0633, -0.42448] },
  { id: 'sbsn', nome: 'Santarém', extent: [-56.975, -4.60564, -52.623, -0.25364] },
  { id: 'sbtf', nome: 'Tefé', extent: [-66.8692, -5.5487, -62.5172, -1.1967] },
  { id: 'sbtt', nome: 'Tabatinga', extent: [-72.111, -6.42439, -67.759, -2.07239] },
  { id: 'sbua', nome: 'São Gabriel da Cachoeira', extent: [-69.233, -2.31968, -64.881, 2.03232] },
];

/**
 * Janela publicada: 48 h mais 30 min de margem.
 *
 * A margem existe por causa do loop do AmaView: ele termina na última imagem
 * do SATÉLITE e casa cada quadro com a varredura de radar mais próxima, até
 * 12 min de distância. Sem folga, o primeiro quadro de um loop de 48 h
 * procuraria uma varredura que acabou de sair da janela.
 */
export const JANELA_H = 48;
export const MARGEM_MIN = 30;

/** Divergência entre a WFS e a tabela que merece aviso no log, em graus. */
export const TOLERANCIA_EXTENT = 0.01;

export function chave(id, produto) {
  return `${id}/${produto}`;
}

// ---------------------------------------------------------------------------
// Nome do arquivo ↔ instante
// ---------------------------------------------------------------------------

const RE_NOME = /^(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})_(\d{2})\.png$/;

const dois = (n) => String(n).padStart(2, '0');

/**
 * Instante (segundos epoch, UTC) → nome do arquivo no SIPAM, `AAAA_MM_DD_HH_MM_SS.png`.
 *
 * É a mesma conta que o AmaView faz para chegar ao arquivo: o manifesto
 * publica só `t`, e o nome sai daqui. Por isso os SEGUNDOS entram — Belém
 * grava `2026_09_21_12_10_06.png`, e arredondar para o minuto quebraria o
 * caminho.
 */
export function nomeDeT(t) {
  if (!Number.isInteger(t)) return null;
  const d = new Date(t * 1000);
  if (!Number.isFinite(d.getTime())) return null;
  return (
    `${d.getUTCFullYear()}_${dois(d.getUTCMonth() + 1)}_${dois(d.getUTCDate())}_` +
    `${dois(d.getUTCHours())}_${dois(d.getUTCMinutes())}_${dois(d.getUTCSeconds())}.png`
  );
}

/** Nome do arquivo → instante em segundos epoch UTC, ou `null` se não for um nome de varredura. */
export function tDeNome(nome) {
  if (typeof nome !== 'string') return null;
  const m = RE_NOME.exec(nome);
  if (!m) return null;
  const [a, mes, d, h, mi, s] = m.slice(1).map(Number);
  const t = Date.UTC(a, mes - 1, d, h, mi, s) / 1000;
  // `Date.UTC` normaliza 31/02 para 03/03 em silêncio. A volta confirma que o
  // nome era uma data de verdade — senão o cliente reconstruiria outro nome.
  return nomeDeT(t) === nome ? t : null;
}

// ---------------------------------------------------------------------------
// Listagem do Apache
// ---------------------------------------------------------------------------

/** "6.6K" / " 20K" / "436" / "1.2M" → bytes (aproximado, como o Apache arredonda). "-" → null. */
export function bytesDe(texto) {
  const m = /^(\d+(?:\.\d+)?)([KMG])?$/i.exec(String(texto ?? '').trim());
  if (!m) return null;
  const mult = { K: 1024, M: 1024 ** 2, G: 1024 ** 3 }[(m[2] ?? '').toUpperCase()] ?? 1;
  return Math.round(Number(m[1]) * mult);
}

/**
 * HTML do autoindex do Apache → `[{ nome, t, bytes }]`, em ordem de `t`.
 *
 * Só entra o que tem nome de varredura: "Parent Directory", subdiretórios
 * (`dbz/`) e os links de ordenação (`?C=N;O=D`) ficam de fora pela forma do
 * nome, não pela posição na tabela. O tamanho vem da mesma linha quando
 * existe — ele permite pular arquivo de 0 byte sem baixá-lo —, e é `null`
 * numa listagem sem tabela.
 */
export function parseListagem(html) {
  if (typeof html !== 'string') return [];
  const itens = new Map();
  for (const m of html.matchAll(/href\s*=\s*"([^"]+)"/gi)) {
    const t = tDeNome(m[1]);
    if (t !== null && !itens.has(m[1])) itens.set(m[1], { nome: m[1], t, bytes: null });
  }
  for (const linha of html.match(/<tr[\s>][\s\S]*?<\/tr>/gi) ?? []) {
    const href = /href\s*=\s*"([^"]+)"/i.exec(linha);
    const item = href && itens.get(href[1]);
    if (!item) continue;
    const celulas = [...linha.matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((c) =>
      c[1].replace(/<[^>]*>/g, '').replace(/&nbsp;/g, ' ').trim(),
    );
    // A coluna de tamanho é a primeira célula com cara de tamanho: "Last
    // modified" (`2026-09-21 09:32`) e o nome não passam no padrão.
    for (const c of celulas) {
      const b = bytesDe(c);
      if (b !== null) {
        item.bytes = b;
        break;
      }
    }
  }
  return [...itens.values()].sort((a, b) => a.t - b.t);
}

// ---------------------------------------------------------------------------
// Janela
// ---------------------------------------------------------------------------

/** Instante mais antigo publicado, em segundos: agora − janela − margem. */
export function limiteDaJanela(agoraMs, janelaH = JANELA_H, margemMin = MARGEM_MIN) {
  return Math.floor(agoraMs / 1000) - Math.round(janelaH * 3600) - Math.round(margemMin * 60);
}

/** A borda é inclusiva: a varredura exatamente no limite entra. */
export function naJanela(t, desde) {
  return Number.isInteger(t) && t >= desde;
}

// ---------------------------------------------------------------------------
// PNG
// ---------------------------------------------------------------------------

const ASSINATURA = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];
const IEND = [0, 0, 0, 0, 0x49, 0x45, 0x4e, 0x44, 0xae, 0x42, 0x60, 0x82];

/**
 * Largura e altura do cabeçalho IHDR, ou `null` se os bytes não começam como
 * um PNG. Precisa só dos primeiros 24 bytes.
 */
export function lerIHDR(buf) {
  if (!buf || typeof buf.length !== 'number' || buf.length < 24) return null;
  for (let i = 0; i < 8; i++) if (buf[i] !== ASSINATURA[i]) return null;
  const u32 = (o) => ((buf[o] << 24) >>> 0) + (buf[o + 1] << 16) + (buf[o + 2] << 8) + buf[o + 3];
  // O IHDR é, por norma, o primeiro bloco, com 13 bytes de dados.
  if (u32(8) !== 13) return null;
  if (String.fromCharCode(buf[12], buf[13], buf[14], buf[15]) !== 'IHDR') return null;
  const w = u32(16);
  const h = u32(20);
  if (w < 1 || h < 1 || w > 100_000 || h > 100_000) return null;
  return { w, h };
}

/**
 * O arquivo é um PNG inteiro?
 *
 * O começo (assinatura + IHDR) separa PNG de HTML de erro servido com 200. O
 * FIM (o bloco IEND) separa PNG inteiro de download cortado — um PNG pela
 * metade decodifica sem erro em vários navegadores, só que com a metade de
 * baixo transparente, e passaria por "sem chuva".
 */
export function pngValido(buf) {
  if (lerIHDR(buf) === null || buf.length < 8 + 25 + 12) return false;
  const o = buf.length - IEND.length;
  for (let i = 0; i < IEND.length; i++) if (buf[o + i] !== IEND[i]) return false;
  return true;
}

// ---------------------------------------------------------------------------
// Extensão (WFS de cobertura)
// ---------------------------------------------------------------------------

/** [minx, miny, maxx, maxy] de qualquer geometria GeoJSON, ou `null`. */
export function bboxDeGeometria(geom) {
  let x0 = Infinity;
  let y0 = Infinity;
  let x1 = -Infinity;
  let y1 = -Infinity;
  const andar = (c) => {
    if (!Array.isArray(c)) return;
    if (typeof c[0] === 'number') {
      const [x, y] = c;
      if (!Number.isFinite(x) || !Number.isFinite(y)) return;
      if (x < x0) x0 = x;
      if (x > x1) x1 = x;
      if (y < y0) y0 = y;
      if (y > y1) y1 = y;
      return;
    }
    for (const filho of c) andar(filho);
  };
  if (geom?.type === 'GeometryCollection') for (const g of geom.geometries ?? []) andar(g?.coordinates);
  else andar(geom?.coordinates);
  return Number.isFinite(x0) ? [x0, y0, x1, y1] : null;
}

/**
 * Uma extensão aceitável para um radar do SIPAM?
 *
 * Quatro números finitos, largura e altura entre 1° e 10° (medido: 4,352° em
 * todos) e dentro da Amazônia Legal com folga. O último teste pega a troca
 * de eixos lat/lon — a armadilha clássica do EPSG:4326 em WFS 2.0, onde o
 * GeoServer pode passar a devolver [lat, lon] depois de uma atualização. Uma
 * extensão trocada passaria no teste de tamanho e poria o radar no oceano.
 */
export function extentValida(e) {
  if (!Array.isArray(e) || e.length !== 4 || !e.every((v) => typeof v === 'number' && Number.isFinite(v))) return false;
  const [x0, y0, x1, y1] = e;
  const larg = x1 - x0;
  const alt = y1 - y0;
  if (!(larg >= 1 && larg <= 10 && alt >= 1 && alt <= 10)) return false;
  return x0 >= -80 && x1 <= -30 && y0 >= -20 && y1 <= 10;
}

/** GeoJSON da WFS → Map id → bbox. Uma feição por radar (propriedade `radar`); repetidas valem a primeira. */
export function extentsDaWfs(geojson) {
  const saida = new Map();
  for (const f of geojson?.features ?? []) {
    const id = typeof f?.properties?.radar === 'string' ? f.properties.radar.trim().toLowerCase() : null;
    if (!id || saida.has(id)) continue;
    const bbox = bboxDeGeometria(f.geometry);
    if (bbox) saida.set(id, bbox);
  }
  return saida;
}

const arred6 = (v) => Math.round(v * 1e6) / 1e6;

/**
 * A extensão a publicar para um radar: a da WFS quando válida, senão a da
 * tabela. `divergencia` é a maior diferença entre as duas, em graus, para o
 * log avisar quando o SIPAM mudar o recorte.
 */
export function escolherExtent(radar, daWfs) {
  if (extentValida(daWfs)) {
    const extent = daWfs.map(arred6);
    const divergencia = Math.max(...extent.map((v, i) => Math.abs(v - radar.extent[i])));
    return { extent, fonte: 'wfs', divergencia: arred6(divergencia) };
  }
  return { extent: [...radar.extent], fonte: 'tabela', divergencia: null };
}

// ---------------------------------------------------------------------------
// Planejamento
// ---------------------------------------------------------------------------

/** Manifesto publicado → Map chave → Set de instantes. `null` se não for um manifesto. */
export function instantesDoManifesto(m) {
  if (!m || !Array.isArray(m.radares)) return null;
  const saida = new Map();
  for (const r of m.radares) {
    if (typeof r?.id !== 'string') continue;
    for (const [p, e] of Object.entries(r.produtos ?? {})) {
      saida.set(chave(r.id, p), new Set((e?.t ?? []).filter(Number.isInteger)));
    }
  }
  return saida;
}

/**
 * Decide, para cada radar e produto, o que reaproveitar do depósito, o que
 * podar e o que buscar — e de onde.
 *
 * - `deposito`: Map chave → Set de instantes com PNG válido no depósito;
 * - `listagens`: Map chave → `{ itens }` (listagem do SIPAM) ou `{ erro }`;
 * - `noAr`: Map chave → Set de instantes do manifesto publicado, ou `null`;
 * - `desde`: limite inferior da janela, em segundos.
 *
 * Um instante entra se estiver na janela e em QUALQUER uma das três fontes:
 * no depósito é reaproveitado; no ar (e não no depósito) vem do Pages — o
 * caso do cache despejado —; só na listagem vem do SIPAM. Listagem que
 * falhou marca `herdado` e não gera busca no SIPAM: o radar fica com o que
 * já tínhamos, que é o depósito ou, se ele se perdeu, o que está no ar.
 *
 * A `fila` sai do MAIS NOVO para o mais antigo, misturando radares: com o
 * prazo do ciclo, o que não couber é o fim da janela, que ninguém olha
 * primeiro, e não um radar inteiro. Na estreia a janela se preenche para trás
 * em poucos ciclos.
 */
export function planejar({ radares = RADARES, produtos = PRODUTOS, deposito, listagens, noAr = null, desde }) {
  const porChave = new Map();
  const fila = [];
  const crescente = (a, b) => a - b;

  for (const r of radares) {
    for (const p of produtos) {
      const k = chave(r.id, p);
      const dep = deposito?.get(k) ?? new Set();
      const ar = noAr?.get(k) ?? new Set();
      const lst = listagens?.get(k);
      const herdado = !lst || !Array.isArray(lst.itens);

      const naListagem = new Set();
      if (!herdado) {
        for (const it of lst.itens) {
          // Arquivo de 0 byte na listagem é produto que o SIPAM não conseguiu
          // gerar. Baixá-lo só daria um PNG inválido — a cada ciclo, para sempre.
          if (it.bytes === 0) continue;
          if (naJanela(it.t, desde)) naListagem.add(it.t);
        }
      }

      const reaproveitar = [];
      const podar = [];
      for (const t of dep) (naJanela(t, desde) ? reaproveitar : podar).push(t);

      const buscar = [];
      const candidatos = new Set(naListagem);
      for (const t of ar) if (naJanela(t, desde)) candidatos.add(t);
      for (const t of candidatos) {
        if (dep.has(t)) continue;
        const item = { id: r.id, produto: p, t, fonte: ar.has(t) ? 'pages' : 'sipam', naListagem: naListagem.has(t) };
        buscar.push(item);
        fila.push(item);
      }

      porChave.set(k, {
        id: r.id,
        produto: p,
        herdado,
        erro: herdado ? String(lst?.erro ?? 'sem listagem') : null,
        listados: naListagem.size,
        reaproveitar: reaproveitar.sort(crescente),
        podar: podar.sort(crescente),
        buscar: buscar.sort((a, b) => a.t - b.t),
      });
    }
  }

  fila.sort(
    (a, b) => b.t - a.t || (a.id < b.id ? -1 : a.id > b.id ? 1 : 0) || produtos.indexOf(a.produto) - produtos.indexOf(b.produto),
  );
  return { porChave, fila };
}

// ---------------------------------------------------------------------------
// Manifesto
// ---------------------------------------------------------------------------

/**
 * Monta o manifesto publicado — o contrato com o AmaView.
 *
 * Cada `radares[i].produtos[p].t` chega como qualquer coleção de instantes;
 * sai crescente, sem repetição, só com inteiros, só dentro da janela (se
 * `desde` vier) e só com o que `existe` confirmar. O cliente reconstrói o
 * nome do arquivo a partir de `t`: um instante sem arquivo seria um 404 no
 * meio da animação.
 *
 * Radares em ordem alfabética de id; todo produto aparece em todo radar,
 * mesmo vazio (sbmq e sbtf hoje).
 */
export function montarManifesto({ gerado, janelaHoras = JANELA_H, produtos = PRODUTOS, radares, existe = null, desde = null }) {
  const ordenados = [...radares].sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
  return {
    gerado,
    fonte: FONTE,
    fonteUrl: FONTE_URL,
    janelaHoras,
    produtos: [...produtos],
    radares: ordenados.map((r) => ({
      id: r.id,
      nome: r.nome,
      extent: r.extent,
      extentFonte: r.extentFonte,
      px: r.px ?? null,
      produtos: Object.fromEntries(
        produtos.map((p) => {
          const e = r.produtos?.[p] ?? {};
          const t = [...new Set(e.t ?? [])]
            .filter((v) => Number.isInteger(v))
            .filter((v) => desde === null || v >= desde)
            .filter((v) => !existe || existe(r.id, p, v))
            .sort((a, b) => a - b);
          return [p, { t, herdado: !!e.herdado, erro: e.erro ?? null }];
        }),
      ),
    })),
  };
}

/** Quantos arquivos um manifesto publica. */
export function contarArquivos(m) {
  let n = 0;
  for (const r of m?.radares ?? []) for (const e of Object.values(r.produtos ?? {})) n += e?.t?.length ?? 0;
  return n;
}

/** Mediana do intervalo entre varreduras consecutivas, em segundos (só para o log). `null` com menos de duas. */
export function medianaIntervalo(ts) {
  const v = [...new Set(ts ?? [])].filter(Number.isFinite).sort((a, b) => a - b);
  if (v.length < 2) return null;
  const d = [];
  for (let i = 1; i < v.length; i++) d.push(v[i] - v[i - 1]);
  d.sort((a, b) => a - b);
  const m = d.length >> 1;
  return d.length % 2 ? d[m] : (d[m - 1] + d[m]) / 2;
}

// ---------------------------------------------------------------------------
// TLS
// ---------------------------------------------------------------------------

/** Códigos do OpenSSL/Node para certificado que não fecha a cadeia. */
const CODIGOS_TLS = new Set([
  'UNABLE_TO_VERIFY_LEAF_SIGNATURE',
  'UNABLE_TO_GET_ISSUER_CERT',
  'UNABLE_TO_GET_ISSUER_CERT_LOCALLY',
  'SELF_SIGNED_CERT_IN_CHAIN',
  'DEPTH_ZERO_SELF_SIGNED_CERT',
  'CERT_HAS_EXPIRED',
  'CERT_NOT_YET_VALID',
  'CERT_UNTRUSTED',
  'CERT_SIGNATURE_FAILURE',
  'ERR_TLS_CERT_ALTNAME_INVALID',
]);

/**
 * O erro do `fetch` é de certificado?
 *
 * O `fetch` do Node embrulha tudo em `TypeError: fetch failed`; o motivo
 * real vem em `cause` — medido contra uma cadeia incompleta:
 * `UNABLE_TO_VERIFY_LEAF_SIGNATURE`, "unable to verify the first certificate".
 */
export function erroDeTls(e) {
  for (let c = e, i = 0; c && i < 4; c = c.cause, i++) {
    if (CODIGOS_TLS.has(c.code)) return true;
    if (/certificate|\bcert\b|self[- ]signed/i.test(c.message ?? '')) return true;
  }
  return false;
}
