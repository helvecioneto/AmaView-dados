/**
 * Conversa com a telemetria da ANA (Agência Nacional de Águas e Saneamento
 * Básico). Tudo que toca a rede está aqui.
 *
 * Base: `http://telemetriaws1.ana.gov.br/ServiceANA.asmx` — o webservice
 * legado, em HTTP puro, sem token e sem cadastro. Aceita GET simples: não é
 * preciso montar envelope SOAP.
 *
 * O HidroWebService novo (`ana.gov.br/hidrowebservice`) exige credencial e
 * devolveu `401 "Usuário sem permissão de acesso ao módulo"` nos testes. Se um
 * dia houver cadastro, ele aceita várias estações por chamada e vale a troca.
 *
 * Por que isto roda no pipeline e não no navegador: o serviço é HTTP sem TLS,
 * e uma página HTTPS no GitHub Pages bloqueia conteúdo misto. Além disso vale
 * a mesma regra das outras camadas — quem consome a fonte é o workflow, não os
 * visitantes.
 */

const BASE = process.env.ANA_BASE || 'http://telemetriaws1.ana.gov.br/ServiceANA.asmx';

/**
 * Concorrência máxima contra a ANA.
 *
 * Medido em 20/09/2026: com 3 requisições simultâneas, 30 estações em 30 s e
 * **zero erros**. Com 6, a taxa CAI para 0,43 est/s e 36 % das chamadas falham
 * — o servidor degrada em vez de enfileirar. Três é o ponto de equilíbrio, e
 * subir daqui piora o resultado além de maltratar um serviço público.
 */
export const CONCORRENCIA = 3;

/**
 * Fuso em que a ANA publica `DataHora`: **Brasília (UTC−3)**, para todas as
 * estações, inclusive as que ficam em UTC−4.
 *
 * Isto foi MEDIDO, não suposto, porque errar aqui desalinharia a camada dos
 * quadros do GOES em três horas. Comparando a leitura mais recente de quatro
 * estações com o relógio, em 21/09/2026 02:36 UTC:
 *
 * | Estação | Lendo como UTC | Como UTC−3 | Como hora local da estação |
 * |---|---|---|---|
 * | Manaus (UTC−4) | 231 min | **51 min** | −9 min (futuro) |
 * | Porto Velho (UTC−4) | 201 min | **21 min** | −39 min (futuro) |
 * | Coroatá (UTC−3) | 231 min | **51 min** | 51 min |
 * | Macapá (UTC−3) | 246 min | **66 min** | 66 min |
 *
 * Hora local dá dado do FUTURO nas estações de UTC−4, o que é impossível; UTC
 * dá quatro horas de atraso, implausível para telemetria de 15 em 15 minutos.
 * Só UTC−3 é coerente nas quatro.
 */
export const FUSO_ANA_MS = -3 * 60 * 60 * 1000;

/** `DataHora` da ANA → epoch UTC. Aceita `2026-09-19 00:00:00` e a forma ISO. */
export function instanteDe(dataHora) {
  if (typeof dataHora !== 'string') return null;
  const texto = dataHora.trim().replace(' ', 'T');
  // Quando o serviço manda o deslocamento, ele manda certo: respeitamos.
  if (/(?:Z|[+-]\d\d:?\d\d)$/.test(texto)) {
    const ms = Date.parse(texto);
    return Number.isFinite(ms) ? ms : null;
  }
  const comoUtc = Date.parse(`${texto}Z`);
  return Number.isFinite(comoUtc) ? comoUtc - FUSO_ANA_MS : null;
}

/** `dd/MM/yyyy`, como o serviço exige. */
export function dataBR(ms) {
  const d = new Date(ms);
  const p = (n) => String(n).padStart(2, '0');
  return `${p(d.getUTCDate())}/${p(d.getUTCMonth() + 1)}/${d.getUTCFullYear()}`;
}

/**
 * Uma leitura da telemetria.
 * `nivel` em cm, `vazao` em m³/s, `ms` é o instante em epoch.
 */
function lerLinha(bloco) {
  const campo = (nome) => {
    const m = new RegExp(`<${nome}>([^<]*)</${nome}>`).exec(bloco);
    return m ? m[1].trim() : null;
  };
  const num = (nome) => {
    const v = campo(nome);
    if (v === null || v === '') return null;
    const n = Number(v.replace(',', '.'));
    return Number.isFinite(n) ? n : null;
  };

  const dataHora = campo('DataHora');
  if (!dataHora) return null;
  const ms = instanteDe(dataHora);
  if (ms === null) return null;

  return {
    ms,
    nivel: num('NivelFinal'),
    vazao: num('VazaoFinal'),
    // O controle de qualidade do próprio serviço: "Dado aprovado" e afins.
    cqNivel: campo('CQ_NivelFinal'),
  };
}

/**
 * Leituras de uma estação numa janela.
 *
 * **A janela não pode passar de ~2 dias.** Três dias devolveram HTTP 500 com
 * `SqlException: Execution Timeout Expired` depois de 30 s; 1 a 2 dias
 * respondem em 0,2–1 s. Para histórico, fatiar mês a mês.
 */
export async function leituras(cod, inicioMs, fimMs, { tentativas = 2, timeoutMs = 45_000 } = {}) {
  const url =
    `${BASE}/DadosHidrometeorologicosGerais` +
    `?codEstacao=${encodeURIComponent(cod)}&dataInicio=${dataBR(inicioMs)}&dataFim=${dataBR(fimMs)}`;

  let ultimoErro = null;
  for (let t = 0; t <= tentativas; t++) {
    try {
      const r = await fetch(url, {
        signal: AbortSignal.timeout(timeoutMs),
        headers: { 'User-Agent': 'AmaView/1.0 (+https://helvecioneto.github.io/AmaView/)' },
      });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      const xml = await r.text();

      // O elemento de linha vem GRAFADO ERRADO no XML da ANA
      // ("DadosHidrometereologicos", com um "e" a mais). É exatamente aqui
      // que a maioria dos parsers quebra. Aceitamos as duas grafias.
      const blocos = xml.match(/<DadosHidromete\w*ologicos[^>]*>[\s\S]*?<\/DadosHidromete\w*ologicos>/g) ?? [];
      const linhas = blocos.map(lerLinha).filter((l) => l !== null);
      linhas.sort((a, b) => a.ms - b.ms);
      return linhas;
    } catch (e) {
      ultimoErro = e;
      if (t < tentativas) await pausa(1500 * (t + 1));
    }
  }
  throw new Error(`estação ${cod}: ${ultimoErro?.message ?? 'falhou'}`);
}

/**
 * Roda `tarefa` sobre `itens` com concorrência limitada, devolvendo
 * `{ ok, erros }`. Uma estação que falha não derruba a rodada: o ciclo publica
 * o que conseguiu e mantém o valor anterior das outras.
 */
export async function emLotes(itens, tarefa, { concorrencia = CONCORRENCIA, aoAndar = null } = {}) {
  const ok = [];
  const erros = [];
  let i = 0;
  let feitos = 0;

  async function trabalhador() {
    for (;;) {
      const meu = i++;
      if (meu >= itens.length) return;
      try {
        const r = await tarefa(itens[meu]);
        if (r !== undefined && r !== null) ok.push(r);
      } catch (e) {
        erros.push({ item: itens[meu], erro: e.message });
      }
      feitos++;
      if (aoAndar && feitos % 20 === 0) aoAndar(feitos, itens.length);
    }
  }

  await Promise.all(Array.from({ length: Math.min(concorrencia, itens.length) }, trabalhador));
  return { ok, erros };
}

function pausa(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

/**
 * Situação declarada pelo Sistema de Alerta de Eventos Críticos do SGB-CPRM.
 *
 * Complementa a ANA com uma classificação que ela não dá: se a cota está
 * normal, em alerta de cheia ou de seca, segundo quem opera a bacia. São
 * poucas estações, mas são as mais importantes (Manaus, Óbidos, Itacoatiara…).
 *
 * Não é essencial: falha aqui devolve mapa vazio e o ciclo segue.
 */
export async function situacoesSace(bacias = ['amazonas_8']) {
  const saida = new Map();
  for (const bacia of bacias) {
    try {
      const r = await fetch(`https://sace.sgb.gov.br/api/dados/${bacia}_cota.csv`, {
        signal: AbortSignal.timeout(30_000),
      });
      if (!r.ok) continue;
      const texto = await r.text();
      const linhas = texto.split('\n').filter(Boolean);
      const cab = linhas[0].split(',').map((c) => c.trim().toLowerCase());
      const iCod = cab.findIndex((c) => /codigo|cod_estacao|estacao/.test(c));
      const iSit = cab.findIndex((c) => /situa/.test(c));
      if (iCod < 0 || iSit < 0) continue;
      for (const l of linhas.slice(1)) {
        const c = l.split(',');
        const cod = c[iCod]?.trim();
        const sit = c[iSit]?.trim();
        if (cod && sit) saida.set(cod, sit);
      }
    } catch {
      /* complemento opcional */
    }
  }
  return saida;
}
