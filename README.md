# AmaView · Dados

Cache aberto dos dados da **Rede Observacional do CEMADEN** para a Amazônia
Legal, republicado a cada 10 minutos em formato pronto para animação no tempo.

Consumido por [AmaView](https://helvecioneto.github.io/AmaView/).

**Publicado em:** <https://helvecioneto.github.io/AmaView-dados/>

---

## Fonte e atribuição

> **Chuva:** Dados da Rede Observacional do CEMADEN/MCTI — Centro Nacional de
> Monitoramento e Alertas de Desastres Naturais,
> <https://www.gov.br/cemaden/pt-br>
>
> **Sondagens:** University of Wyoming, Department of Atmospheric Science,
> <https://weather.uwyo.edu/upperair/>
>
> **Nível dos rios:** Agência Nacional de Águas e Saneamento Básico (ANA) —
> rede telemétrica do SNIRH, <https://www.snirh.gov.br/hidroweb/>
>
> **Embarcações (AIS):** Open Waters AIS (<https://openwaters.io/ais/>),
> agregando AISHub (<https://www.aishub.net>) e aisstream.io.
> O crédito é **por fonte** e nunca concatenado — ver
> [`docs/privacidade-embarcacoes.md`](docs/privacidade-embarcacoes.md).

Os Termos de Uso da [Plataforma de Entrega de Dados](https://ped.cemaden.gov.br/suporte/termouso)
declaram o acesso como serviço público gratuito e pedem, em nome da Lei de
Acesso à Informação (Lei 12.527/2011), que a fonte seja citada. Este
repositório **apenas recacheia e reformata**: os dados continuam sendo do
CEMADEN, e quem consumir daqui deve citar o CEMADEN, não o AmaView.

O código deste repositório é MIT. Os **dados** não são cobertos por essa
licença — seguem os termos do CEMADEN.

---

## Por que este repositório existe

Três problemas, resolvidos de uma vez:

1. **CORS.** Nenhuma das fontes do CEMADEN pode ser lida direto do navegador:
   `resources.cemaden.gov.br` não manda cabeçalho de CORS e
   `sws.cemaden.gov.br` só libera o próprio portal. O GitHub Pages serve com
   `Access-Control-Allow-Origin: *`.
2. **Segredo.** O token da API não pode viver num front estático — ficaria
   legível no bundle. Aqui ele é um *secret* do Actions e nunca sai do runner.
3. **Limite de uso.** A PED permite 12 requisições/minuto por usuário. Como
   quem fala com o CEMADEN é este workflow, e não os visitantes, o consumo é
   **fixo em 9 requisições por ciclo** — não importa se o AmaView tem um
   usuário ou cem mil.

O repositório é público para que os minutos de Actions sejam gratuitos e para
que o recache seja auditável por quem quiser conferir a origem do dado.

---

## Formato

### `cemaden/serie.json`

Grade **densa**: uma linha por estação, 144 colunas de 10 minutos cobrindo 24 h.

```jsonc
{
  "fonte": "Dados da Rede Observacional do CEMADEN/MCTI — …",
  "gerado": "2026-09-20T16:20:54.982Z",
  "modo": "aberto",          // "ped" (com token) | "aberto" (sem token)
  "grandeza": "acum24h",     // "chuva10min" | "acum24h"  ← veja abaixo
  "t0": 1789835400000,       // epoch ms UTC da 1ª coluna
  "passoMin": 10,
  "slots": 144,
  "semDado": -1,
  "estacoes": ["130190203A", …],   // mesma ordem de `v`
  "v": [[-1, 0, 0, 2.4, …], …]     // mm
}
```

`-1` é **ausência de medição**, e é diferente de `0`, que é chuva medida igual
a zero. Confundir os dois transforma silêncio de sensor em "não choveu".

Denso, e não esparso, de propósito: a chuva é rara no tempo, quase toda célula
é `0`, e zeros repetidos desaparecem no gzip. Medido em dados reais: **3 KB**
comprimido logo após o primeiro ciclo e **35 KB** com a grade de 24 h cheia,
contra ~33 KB do formato esparso já no caso médio — com a vantagem de o
navegador indexar por posição, sem busca.

### `grandeza` — a diferença que importa

| Valor | Significado | De onde vem |
|---|---|---|
| `chuva10min` | milímetros caídos **naquela fatia de 10 min** | API PED, com token |
| `acum24h` | milímetros acumulados nas **24 h que terminam** naquela fatia | instantâneo aberto, sem token |

São grandezas distintas e não devem ser somadas entre si. O AmaView lê este
campo e muda a leitura da camada conforme o caso — com `chuva10min` ele pode
somar qualquer janela (10 min, 1 h, 3 h, 24 h); com `acum24h` só exibe o valor
como está.

### `cemaden/estacoes.json`

Cadastro, na mesma ordem de `serie.json`. Campos curtos porque o arquivo é
baixado por todo visitante: `c` código, `i` id interno, `n` nome, `u` UF,
`m` município, `b` código IBGE, `y` latitude, `x` longitude, `t` tipo.

### `sondagem/perfis.json`

Perfis verticais de radiossonda das 14 estações da Amazônia Legal, da
Universidade de Wyoming. **Até três sondagens por estação** dentro das últimas
48 h, da mais recente para a mais antiga.

Três, e não uma: a rede lança 2× por dia e o loop do AmaView cobre até 24 h.
Com só a mais recente, metade da animação ficava sem nenhuma sondagem no mapa
— inclusive lançamentos que estavam dentro da janela exibida.

```jsonc
{
  "perfis": [{
    "wmo": 82193,
    "ms": 1789905600000,        // instante do lançamento (null = sem sondagem)
    "niveis": [{ "p": 1012.8, "z": 14, "t": 28, "d": 23.5 }, …],
    "indices": { "PWAT": 42, "MUCAPE": 594.2, "LCLP": 948, … },
    "erro": null                // texto quando a busca falhou
  }]
}
```

`p` hPa, `z` metros, `t` temperatura °C, `d` ponto de orvalho °C.

O perfil bruto tem centenas de níveis (Belém veio com 4.033); guardamos os
obrigatórios da meteorologia mais as quebras bruscas de umidade — ~13 a 24 por
estação, **2,6 KB gzip** para a rede inteira.

`ms: null` com `erro: null` é estação calada, não falha nossa: acontece de
verdade e com frequência. Manaus passou 55 h sem reportar durante o
levantamento.

O `t` de cada ponto da trilha é **relativo a `ms`, e negativo no começo**: o
balão sobe uns 45 min antes da hora cheia para que a amostra da troposfera
média caia nela. Quem consumir precisa tratar o lançamento — e não `ms` — como
o instante em que a sondagem passa a existir.

### `navios/atual.json`

Posições do ciclo e a trilha de 24 h de cada embarcação. A trilha usa
**instante absoluto**, não índice de grade: a janela desliza a cada ciclo, e
uma trilha indexada por posição andaria para trás sozinha a cada publicação.

```jsonc
{
  "gerado": "2026-09-21T02:15:00Z",
  // Um crédito POR FONTE. Licenças não se fundem — nunca concatenar.
  "attribution": { "aishub": "Open Waters AIS (…). AISHub (…)", "aisstream": "…" },
  "trilhaHoras": 24,
  "navios": [
    {
      "m": 710000596,          // MMSI
      "y": -3.1478, "x": -59.9284,
      "s": 9.4,                // velocidade sobre o fundo, nós
      "c": 87.3,               // rumo sobre o fundo, graus
      "h": 85,                 // proa, graus (costuma faltar)
      "n": 0,                  // situação de navegação do AIS
      "v": 1758420000000,      // quando foi ouvida
      "f": "aishub",           // fonte — decide o crédito desta embarcação
      "t": [[1758410000000, -3.14, -59.92, 9.2]],  // [instante, lat, lon, nós]
      "sumiu": false           // true = sem posição nova, fora de alcance
    }
  ],
  // Frota pequena de transponder voluntário: ponto e mais nada.
  "anonimos": [{ "y": -1.45, "x": -48.5, "c": 210, "s": 3.1, "t": 0 }]
}
```

### `navios/cadastro.json`

O estático acumulado, por MMSI: nome, IMO, indicativo, tipo, dimensões, calado,
destino e ETA. Cresce a cada ciclo — cada embarcação retransmite esses campos a
cada ~6 min, e o ciclo colhe o que passar pela janela. `d` é quando o bloco foi
declarado (a interface mostra "declarado há 3 dias" quando o dado envelhece) e
`v` quando a embarcação foi ouvida pela última vez. MMSI sem ser ouvido há 30
dias sai do cadastro.

### `navios/escuta.json`

Células de 0,25° onde houve recepção nos últimos 7 dias, com o instante da
última. É o que permite ao mapa distinguir **"rio vazio"** de **"sem
recepção"** — a leitura errada mais provável desta camada. O AIS terrestre só
alcança perto do receptor, e na Amazônia há receptor em Belém, Manaus e no
Tapajós; entre eles, o rio é cego.

### `rios/atual.json`

Cota de 290 estações telemétricas da ANA na Amazônia Legal (SGB-CPRM,
secretarias estaduais, DNIT e Água e Solo; **sem** as de usinas
hidrelétricas), com série horária de 48 h. A cota vai em **centímetros** (como a ANA publica); a conversão para
metros é da interface.

```jsonc
{
  "gerado": "2026-09-21T08:22:32Z",
  "t0": 1758…, "passoMin": 60, "slots": 48,
  "rios": [
    {
      "c": "14990000",       // código da estação
      "cm": 2063,            // cota da última leitura
      "ms": 1758…,
      "q": null,             // vazão, m³/s
      "d24": -19, "d48": -41,
      "t": "descendo",
      "a": "normal",         // classe da escala divergente
      "p": 29,               // POSIÇÃO PERCENTÍLICA entre os anos medidos
      "an": -192,            // diferença para a mediana desta época, cm
      "ref": { "p50": 2255, "p25": 2010, "p75": 2480, "anos": 13 },
      "ext": { "mn": 1213, "mnd": "2024-11-02", "mx": 3002, "mxd": "2021-06-17", "desde": 2014 }
    }
  ],
  // Uma linha por estação, SEMPRE com `slots` valores; null onde não houve medição.
  "serie": { "14990000": [2082, 2081, null, 2079] }
}
```

`p` é o que a interface usa para colorir, e não `cm`: cada régua tem o seu
próprio zero, então cota de estações diferentes não se compara. `a` e `p` são
`null` nas 49 estações com menos de 8 anos medidos — ausência de referência não
é normalidade.

`ext` são os extremos **desta estação no período medido**, e não o recorde
histórico da régua: a de Manaus tem série desde 1902.

`h: true` marca uma estação que não respondeu nesta rodada e **herdou** a
publicação anterior, com a série deslocada para a grade nova. A coleta tem
prazo global de 5 min (uma ANA lenta chegou a projetar duas horas por
rodada), e a fila é ordenada pela idade da leitura anterior — quem ficou de
fora vai para a frente na rodada seguinte.

### `cemaden/manifest.json`

Resumo do ciclo (instante, grandeza, cobertura, contagens). Pequeno — serve
para sondar se vale a pena rebaixar a série.

---

## Como funciona

Dois workflows, com cadências diferentes porque as fontes são diferentes:

| Workflow | Cadência | O que faz | Por quê |
|---|---|---|---|
| `chuva.yml` | a cada 15 min | CEMADEN **+ embarcações (AIS) + rios (ANA)** | o CEMADEN publica de 10 em 10 min |
| `sondagem.yml` | 4×/dia | radiossonda | o balão sobe 2×/dia e o dado aparece ~7 h depois |

As embarcações rodam **dentro** do ciclo da chuva, e não num workflow próprio.
O grupo `publicar-pages` guarda no máximo uma execução rodando e uma pendente:
quando uma terceira entra, a pendente é **cancelada** — em silêncio, com status
`cancelled` e não `failure`. Um terceiro workflow na mesma cadência comeria
ciclos da chuva sem ninguém perceber. O passo do AIS roda com
`continue-on-error`: se a fonte estiver fora, a chuva publica do mesmo jeito.

O passo dos **rios** roda só de hora em hora, e não a cada ciclo: o nível de um
rio amazônico muda de 5 a 30 cm por DIA, e 154 estações a cada 15 min seriam
~15 mil requisições diárias a um serviço público para um dado que não mudou. O
próprio script confere o carimbo da publicação anterior e sai em silêncio
quando ainda não venceu, avisando o workflow pelo output `rodou`.

Buscar as sondagens a cada 15 min eram **~8.000 requisições diárias** a um
servidor acadêmico sem SLA para um dado que muda duas vezes. Agora são ~340.

Os dois publicam no MESMO site do Pages, e cada publicação substitui o site
inteiro — então cada um restaura a pasta do outro (`preservar.mjs`) antes de
publicar, e ambos usam o mesmo grupo de concorrência para nunca rodarem juntos.
Se a restauração falhar, o workflow falha: publicar sem metade dos dados é pior
que não publicar.

```
cron
   └─ node scripts/build.mjs
        ├─ lê a publicação ANTERIOR pela URL pública (é o estado entre ciclos)
        ├─ com token  → PED /pcds-cadastro + /pcds/dados_rede, 1 par por UF
        │               (9 UFs, espaçadas 6 s → dentro das 12 req/min)
        ├─ sem token  → resources.cemaden.gov.br/dados/311_24.json (JSONP)
        │               e empilha o instantâneo na última coluna
        └─ escreve site/ → artifact do Pages
```

O workflow **não faz commit**. Os arquivos vão direto como artifact, então o
histórico do git não cresce com 144 execuções por dia.

Se qualquer passo falhar, o job para antes de publicar e **a versão anterior
continua no ar** — uma falha temporária do CEMADEN não derruba a camada.

### Sem token o produto já funciona

Sem credencial nenhuma, cada ciclo acrescenta uma coluna real ao final da
grade e descarta a mais antiga. Depois de algumas horas no ar existe uma série
temporal legítima — nenhuma coluna é inventada, cada uma é uma medição
publicada pelo CEMADEN com o instante em que foi publicada. A limitação é a
grandeza: é o acumulado de 24 h, não a chuva de cada 10 minutos.

Com o token, a grade passa a ser remontada inteira a cada ciclo, com o dado
bruto e a granularidade real — inclusive as 24 h anteriores à primeira
execução.

---

## Configuração

### Credenciais da PED (opcional, mas recomendado)

O JWT da PED é de vida curta e o claim `exp` **não é confiável**: medido em
produção, um token recém-emitido anunciou `exp` no passado e mesmo assim foi
aceito, devolvendo as ~19 mil leituras do ciclo. De um jeito ou de outro não dá
para guardar o token como secret num cron de 10 minutos. O que fica guardado
são as **credenciais**, e cada execução pede um token novo, que existe só na
memória do runner.

Por isso o `exp` nunca decide se a chamada será feita — quem dá a palavra final
é a própria API, e uma recusa dela cai na fonte aberta sem derrubar a camada.

1. Cadastre-se em <https://ped.cemaden.gov.br/> → *Cadastrar Usuário*.
2. Neste repositório: **Settings → Secrets and variables → Actions → New
   repository secret**, dois secrets:
   - `CEMADEN_EMAIL` — o e-mail da conta
   - `CEMADEN_SENHA` — a senha da conta

Sem os secrets o workflow roda no modo aberto, sem falhar.

`CEMADEN_TOKEN` ainda é aceito para execução manual e depuração, mas o script
avisa quando ele está perto de expirar e cai na fonte aberta depois disso.

### Cadência de verdade (opcional)

**O agendamento do GitHub Actions não é confiável.** Medido neste repositório:
pedindo um ciclo a cada 15 minutos, o intervalo real entre disparos foi de
**135 minutos em média** (mediana 153). Um pedido anterior de 10 minutos não
disparou uma única vez em 1h42. Isso é comportamento conhecido — o GitHub
descarta agendamentos sob carga, sem aviso e sem repor.

Não há conserto dentro do GitHub:

- **mais linhas de `cron`** ajudam na margem (já são duas), mas continuam
  sujeitas ao mesmo descarte;
- um **workflow que dorme** e se redispara funcionaria, mas queimaria ~22
  horas de runner por dia só esperando. É infraestrutura compartilhada e
  gratuita: gastar isso para dormir é o mesmo desperdício que evitamos ao
  parar de bater no servidor do Wyoming de 15 em 15 minutos.

O conserto é chamar o `workflow_dispatch` **de fora**:

```
POST https://api.github.com/repos/helvecioneto/AmaView-dados/actions/workflows/chuva.yml/dispatches
Authorization: Bearer <TOKEN>
Accept: application/vnd.github+json
Content-Type: application/json

{"ref":"main"}
```

Resposta `204` significa aceito. Testado: o ciclo começa em segundos.

Para montar isso:

1. Crie um **fine-grained PAT** em <https://github.com/settings/tokens?type=beta>
   com acesso só a este repositório e a permissão **Actions: Read and write**.
   Nada além disso — esse token só serve para apertar este botão.
2. Cadastre a chamada num agendador gratuito que rode a cada 15 min
   (<https://cron-job.org>, um Cron Trigger do Cloudflare Workers, ou qualquer
   máquina sua com `cron`).
3. Guarde o token no agendador, nunca no repositório.

Com isso o `schedule` vira só a rede de segurança: se o agendador externo
cair, os disparos irregulares do GitHub ainda mantêm o dado vivo.

O AmaView avisa sozinho quando o dado envelhece (faixa no painel e ponto
amarelo no trilho, acima de 45 min), então uma falha do agendador aparece em
vez de passar batida.

### Pages

**Settings → Pages → Source: GitHub Actions.**

### Variável opcional

`BASE_URL` (Settings → Variables) sobrescreve a URL de onde a publicação
anterior é relida. Só é preciso mudar se o repositório for renomeado.

---

## Desenvolvimento

```sh
node --test scripts/serie.test.mjs   # funções da grade
node scripts/build.mjs               # um ciclo, escreve em site/
```

`scripts/serie.mjs` é puro e testado; `scripts/cemaden.mjs` isola a rede.

---

## Limites de uso respeitados

- **12 req/min** (usuário externo da PED): as chamadas são espaçadas 6 s em
  `cemaden.mjs`, o que mantém o pico dentro do limite, e não só a média.
- **Scroll de 30 s** da paginação: os endpoints paginados não são usados hoje;
  se passarem a ser, paginar sem pausas longas dentro de um mesmo scroll.
- **Conta inativa por 1 ano é removida:** o cron mantém a conta ativa sozinho.

Se o AmaView crescer, vale pedir ao CEMADEN o status de *parceiro*
(180 req/min) — `contato@cemaden.gov.br` ou `semipluv@cemaden.gov.br`.
