/**
 * Conversa com a fonte de AIS (aiscast / Open Waters). Tudo que toca a rede
 * está aqui; o que decide o que é publicado está em `frota.mjs`.
 *
 * Base: https://ais.openwaters.io  ·  código MIT, github.com/openwatersio/aiscast
 *
 * Leitura ANÔNIMA, sem cadastro nem chave. Medido em 20/09/2026: o REST
 * responde `Access-Control-Allow-Origin: *` e o WebSocket aceita `subscribe`
 * sem token, anunciando `role: anonymous` e o teto de 100 graus² de área.
 *
 * `OPENWATERS_TOKEN` é opcional e eleva o nível de anônimo para **pessoal**:
 * área de 100 para 400 graus², 20 para 50 mensagens por segundo, e — o que de
 * fato importa aqui — os limites passam a contar **por token** em vez de por
 * endereço IP. O runner do GitHub Actions tem IP compartilhado com muita
 * gente, então sem token as conexões disputam a mesma cota com estranhos.
 *
 * O token é gerado por API, sem conta e sem formulário: basta um par Ed25519 e
 * um POST em `/v1/keys` com a chave pública em base64url. Ele não expira
 * (`exp: 0`). O ciclo funciona sem ele.
 */

const BASE = process.env.AIS_BASE || 'https://ais.openwaters.io';
const TOKEN = process.env.OPENWATERS_TOKEN?.trim() || null;

/**
 * As quatro caixas que cobrem a navegação amazônica, em
 * [minLat, minLon, maxLat, maxLon].
 *
 * Somam 47,4 graus² — dentro do teto anônimo de 100. Não é a Amazônia Legal
 * inteira (704 graus²) de propósito: uma varredura em grade de toda a região,
 * em 21/09/2026, achou 75 embarcações, das quais 73 já caíam nas quatro caixas
 * originais. Fora delas não há receptor AIS, e assinar área vazia só gastaria
 * o limite.
 */
export const CAIXAS = [
  { nome: 'Manaus / Negro / Solimões', bbox: [-4.6, -62.0, -2.0, -58.0] },
  { nome: 'Santarém / Itaituba / Tapajós', bbox: [-4.6, -57.0, -1.6, -53.5] },
  { nome: 'Belém / estuário / Marajó', bbox: [-2.8, -50.5, 0.8, -46.0] },
  { nome: 'Macapá / foz norte', bbox: [-1.2, -52.0, 2.2, -49.5] },
  // Itaqui e a baía de São Marcos: o grande porto do Maranhão, ainda dentro
  // da Amazônia Legal. Varredura da região inteira em 21/09/2026 achou só
  // duas embarcações aqui fora das quatro caixas — mas são as únicas.
  { nome: 'São Luís / Itaqui', bbox: [-3.2, -45.2, -1.8, -43.9] },
];

function cabecalhos() {
  const h = { 'User-Agent': 'AmaView/1.0 (+https://helvecioneto.github.io/AmaView/)' };
  if (TOKEN) h.Authorization = `Bearer ${TOKEN}`;
  return h;
}

/**
 * Posições de agora, uma requisição por caixa.
 *
 * Esta é a fonte AUTORITATIVA da posição: o snapshot do REST traz todo mundo
 * que a fonte ouviu nos últimos 30 min, enquanto o WebSocket só traz quem
 * transmitir durante a janela em que ficamos escutando.
 */
export async function buscarPosicoes() {
  const porMmsi = new Map();
  const atribuicao = {};
  const porCaixa = [];

  for (const { nome, bbox } of CAIXAS) {
    const url = `${BASE}/v1/vessels?bbox=${bbox.join(',')}`;
    // 15 s: o normal é ~1 s, e quatro caixas a 30 s cada segurariam o ciclo
    // da chuva por dois minutos numa fonte lenta.
    const r = await fetch(url, { headers: cabecalhos(), signal: AbortSignal.timeout(15_000) });
    if (!r.ok) throw new Error(`/v1/vessels ${nome}: HTTP ${r.status}`);
    const j = await r.json();
    if (!Array.isArray(j?.features)) throw new Error(`/v1/vessels ${nome}: resposta sem \`features\``);

    Object.assign(atribuicao, j.attribution ?? {});
    let n = 0;
    for (const f of j.features) {
      const [lon, lat] = f?.geometry?.coordinates ?? [];
      const p = f?.properties ?? {};
      if (!Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(p.mmsi)) continue;
      // A mesma embarcação pode aparecer em duas caixas que se tocam.
      const antes = porMmsi.get(p.mmsi);
      // `seen` no futuro (relógio da fonte errado) nunca expiraria da trilha.
      const visto = Math.min(Date.parse(p.seen ?? '') || 0, Date.now());
      if (antes && antes.visto >= visto) continue;
      porMmsi.set(p.mmsi, {
        mmsi: p.mmsi,
        lat,
        lon,
        sog: num(p.sog),
        cog: num(p.cog),
        heading: num(p.heading),
        navStatus: num(p.nav_status),
        tipo: num(p.type),
        nome: texto(p.name),
        fonte: p.source ?? null,
        // A mensagem que trouxe a posição diz a CLASSE do transponder, que é
        // o que decide se a embarcação é obrigada a transmitir (ver
        // `classificar` em frota.mjs).
        msgType: p.msg_type ?? null,
        visto,
      });
      n++;
    }
    porCaixa.push({ nome, n });
    // Bem abaixo das 120 req/min, e educado com um projeto em beta.
    await pausa(700);
  }

  return { navios: [...porMmsi.values()], atribuicao, porCaixa };
}

/**
 * Colhe `ShipStaticData` do fluxo: nome, IMO, indicativo, tipo, dimensões,
 * calado, destino e ETA — os campos que o snapshot do REST NÃO tem.
 *
 * Fica escutando até `msMax`, mas sai antes se parar de chegar MMSI novo:
 * o `snapshot:true` despeja o que a fonte já tem em cache nos primeiros
 * segundos, e depois disso a chegada é lenta (cada navio retransmite o
 * estático a cada ~6 min).
 *
 * **Nunca lança.** O estático é um acréscimo: se o WebSocket não abrir, o
 * ciclo publica as posições do REST e o cadastro do ciclo anterior, e o que
 * faltou entra no próximo. Derrubar o ciclo por causa disso seria trocar um
 * dado incompleto por nenhum.
 */
export async function colherEstaticos({ msMax = 90_000, msOcioso = 20_000 } = {}) {
  const estaticos = new Map();
  const atribuicao = {};
  const fontes = new Map();
  let mensagens = 0;
  let erro = null;

  await new Promise((resolve) => {
    let ws;
    try {
      // O token vai no cabeçalho `Authorization`, e não no frame de
      // subscrição: mandá-lo dentro do JSON derruba a conexão (medido).
      //
      // `headers` é uma extensão do WebSocket do Node (undici) que o padrão
      // do navegador não tem — e não precisa ter, porque este arquivo só roda
      // no runner. Confirmado: com o cabeçalho, o `welcome` anuncia
      // `role=personal` e `area=400`; sem ele, `anonymous` e `area=100`.
      ws = new WebSocket(`${BASE}/v1/stream`, TOKEN ? { headers: { Authorization: `Bearer ${TOKEN}` } } : undefined);
    } catch (e) {
      erro = e.message;
      resolve();
      return;
    }

    let ultimoNovo = Date.now();
    const encerrar = () => {
      try {
        ws.close();
      } catch {
        /* já fechado */
      }
      clearTimeout(limite);
      clearInterval(ocioso);
      resolve();
    };
    const limite = setTimeout(encerrar, msMax);
    const ocioso = setInterval(() => {
      if (Date.now() - ultimoNovo > msOcioso) encerrar();
    }, 2000);

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'subscribe', bbox: CAIXAS.map((c) => c.bbox), snapshot: true }));
    };
    ws.onerror = (e) => {
      erro = e?.message ?? 'falha no WebSocket';
    };
    ws.onclose = encerrar;
    ws.onmessage = (ev) => {
      mensagens++;
      let m;
      try {
        m = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (m?.type === 'error') {
        erro = m.error ?? 'erro do servidor';
        return;
      }
      if (m?.attribution && m?.source) atribuicao[m.source] = m.attribution;
      if (m?.source) fontes.set(m.source, (fontes.get(m.source) ?? 0) + 1);
      if (m?.msg_type !== 'ShipStaticData' || !Number.isFinite(m.mmsi)) return;

      const d = m.message ?? {};
      if (!estaticos.has(m.mmsi)) ultimoNovo = Date.now();
      estaticos.set(m.mmsi, {
        nome: texto(d.Name),
        indicativo: texto(d.CallSign),
        imo: num(d.ImoNumber),
        tipo: num(d.Type),
        dim: d.Dimension ?? null,
        calado: num(d.MaximumStaticDraught),
        destino: texto(d.Destination),
        eta: d.Eta ?? null,
        fonte: m.source ?? null,
      });
    };
  });

  return { estaticos, atribuicao, fontes: [...fontes], mensagens, erro };
}

function num(x) {
  return typeof x === 'number' && Number.isFinite(x) ? x : null;
}

function texto(x) {
  if (typeof x !== 'string') return null;
  const t = x.replace(/\u0000/g, '').replace(/@+$/, '').trim();
  return t.length ? t : null;
}

function pausa(ms) {
  return new Promise((r) => setTimeout(r, ms));
}
