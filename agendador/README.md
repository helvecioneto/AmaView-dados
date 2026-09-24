# Agendador externo

O agendamento do GitHub Actions **não é confiável**. Medido neste repositório:
pedindo um ciclo a cada 15 minutos, o intervalo real entre disparos foi de
**135 minutos em média** (mediana 153). Um pedido anterior de 10 minutos não
disparou uma única vez em 1h42.

Isto aqui é o conserto: uma máquina com relógio de verdade aperta o botão do
`workflow_dispatch` na hora certa. Um timer por workflow:

| Unidade | Quando | Workflow |
|---|---|---|
| `amaview-chuva.timer` | a cada 15 min | `chuva.yml` |
| `amaview-sondagem.timer` | 07:22, 10:22, 19:22 e 22:22 UTC | `sondagem.yml` |

A sondagem entrou depois: dependia só do `schedule` do GitHub e, em
21/09/2026, passou 12 h sem atualizar.

O `schedule` do workflow continua ligado como **rede de segurança** — se esta
máquina cair, os disparos irregulares do GitHub ainda mantêm o dado vivo, e o
AmaView avisa quando ele envelhece.

## Por que systemd, e não cron

A máquina nem tem `cron` instalada, mas isso é detalhe. O timer do systemd é
melhor para este trabalho:

- `Persistent=true` **recupera o disparo perdido** depois de reinício ou queda
  de rede, que é exatamente o que o GitHub não faz;
- o log vai para o journald (`journalctl -u amaview-chuva -u amaview-sondagem`),
  com código de saída de cada execução;
- `RandomizedDelaySec` desloca do minuto cheio, que é quando todo mundo agenda.

## Uso

Tudo pelo `amaview`, que fica em `~` e em `/usr/local/bin`:

| Comando | O que faz |
|---|---|
| `amaview` | situação: timers, token, últimas execuções e frescor do dado |
| `amaview instalar` | clona/atualiza, instala as unidades e liga os timers |
| `amaview token` | grava o token (pede na tela, sem eco) |
| `amaview chave purpleair` / `amaview chave openaq` | grava a chave opcional da qualidade do ar (sem eco; testa com uma chamada grátis antes; `… remover` apaga) |
| `amaview disparar [workflow]` | dispara um ciclo agora (`chuva.yml`, ou `sondagem.yml`) |
| `amaview logs [n]` | últimas execuções |
| `amaview parar` | desliga os timers |
| `amaview remover` | desinstala (mantém o token) |

### Primeira vez

```sh
~/amaview instalar
~/amaview token      # cola o PAT; ele testa e liga os timers sozinho
```

O PAT vem de <https://github.com/settings/tokens?type=beta>, com acesso só a
este repositório e permissão **Actions: Read and write**.

### Atualizar

```sh
amaview instalar     # git pull + reinstala as unidades e o próprio script, e religa os timers
```

## Blocos do GOES-19

A mesma máquina corta os JPEGs do setor NSA do [NOAA/STAR](https://www.star.nesdis.noaa.gov/goes/sector.php?sat=G19&sector=nsa)
em blocos de 512 px (larguras 3600 e 7200), para o AmaView baixar e decodificar
só a parte visível da imagem. No zoom 6, por exemplo, a tela mostra 0,8% do
quadro de 7200 px: ~0,4 MB em blocos contra 7,3 MB do arquivo inteiro.

- **Sem recompressão.** O corte é feito nos coeficientes do JPEG (`tjTransform`
  da TurboJPEG, o mesmo do `jpegtran -crop`): os pixels são os do arquivo da
  NOAA. Os 135 blocos de um quadro de 7200 px saem numa chamada (~0,2 s).
- **Sobra de 16 px** do vizinho em cada lado do bloco (múltiplo do MCU, então
  ainda sem perda). O AmaView desenha só o miolo de 512 px; a suavização ao
  ampliar lê a sobra e não aparecem emendas. Custa ~13% a mais de bytes.
- **Pré-corte.** A cada minuto, um HEAD no arquivo de 450 px de cada horário
  que falta nas últimas 6 h (grade de 10 min, sem baixar a listagem de 1,1 MB)
  diz se o STAR o publicou; publicado, todos os produtos dele são cortados na
  hora, e o produto que ainda não chegou é tentado de novo a cada minuto. O
  `latest.jpg` do STAR não serve de sinal: em 22/09/2026 o GOES-19 parou das
  09:50 às 13h (manutenção no solo da NOAA) e voltou soltando os quadros das
  12:00–12:20 sem mexer nele. O resto das últimas 48 h é preenchido do mais
  novo para o mais velho. Em regime: ~240 MB baixados do STAR a cada 10 min,
  ~85 GB em disco.
- **`/blocos/v1/ultimos`**: o horário mais recente já cortado de cada produto.
  O AmaView pergunta a cada minuto e recarrega as listagens quando há quadro
  novo — sem depender do `latest.jpg`.
- **Sob demanda.** O nginx serve o bloco do disco; se ainda não existe (horário
  fora da grade, pré-corte atrasado), o pedido cai no `blocos.py`, que corta o
  quadro na hora e devolve o bloco (~2 s, quase tudo download).
- **Se esta máquina cair**, o AmaView volta sozinho ao JPEG inteiro do STAR.

| Peça | Onde |
|---|---|
| `blocos/blocos.py` | serviço (só local, porta 8090), pré-corte e limpeza (> 49 h) |
| `blocos/amaview-blocos.service` | unidade systemd (usuário `amaview-blocos`, `/var/cache/amaview-blocos`) |
| `blocos/nginx-*.conf` | site do nginx: disco primeiro, `@blocos` no que falta; CORS e cache imutável |

URL: `/blocos/v1/nsa/{produto}/{AAAADDDHHMM}/{largura}/{linha}_{coluna}.jpg`
(dia juliano, UTC, como nos arquivos do STAR). Saúde: `/blocos/saude`.

O `amaview instalar` instala e atualiza tudo; `amaview` mostra a situação. O
HTTPS (o AmaView é servido por HTTPS e o navegador recusa blocos por HTTP) tem
dois nomes:

- **`https://147.15.84.134`, o que o AmaView usa.** Certificado de IP da Let's
  Encrypt (perfil `shortlived`, ~6 dias), emitido e renovado pelo
  [`lego`](https://go-acme.github.io/lego/) (versão e checksum fixos no
  `amaview`; o certbot 2.9 do Ubuntu não emite para IP) no
  `amaview-certificado-ip.timer`, duas vezes por dia; `amaview renovar-ip` faz
  na mão. Motivo: em 23/09/2026 uma rede com filtro FortiGuard desviava o DNS
  de `sslip.io` e `nip.io` para a página de bloqueio (208.91.112.55) — blocos e
  fumaça sumiam para quem estava atrás dela, mas o IP passava.
- `https://147-15-84-134.sslip.io`, o nome antigo, continua no ar
  (`amaview certificado`, uma vez; o `certbot.timer` renova).

Testes (sem rede): `python3 -m unittest discover -s agendador/blocos`.

## Fumaça do GOES-19

A mesma máquina transforma a máscara de fumaça do produto **ABI-L2-ADPF** da
NOAA (detecção de aerossóis, disco completo, a cada 10 min; balde público
[`noaa-goes19`](https://noaa-goes19.s3.amazonaws.com/index.html#ABI-L2-ADPF/))
em contornos para o AmaView.

- **Só o que a NOAA marcou, onde ela garante a qualidade.** `Smoke == 1` com
  o sol na faixa quantitativa do algoritmo (zênite < 60°, `PQI1`) vira
  polígono, sem outro filtro nem reclassificação. Com o sol baixo (60–87°, a
  faixa que a NOAA chama de degradada) o ADP marca o terminador inteiro: em
  23/09/2026 09:10 UTC eram 264 mil km² de "fumaça" ao amanhecer, 99,9% nessa
  faixa. O contorno é a borda dos próprios pixels (união dos
  quadrados com o `shapely`), na grade de 2 km do produto: cobre exatamente o
  que a NOAA marcou. Vértices colineares saem; coordenadas em lon/lat com 3
  casas (~100 m).
- **Só a América do Sul do setor NSA** (a oeste de 30°W). A leste, o setor
  mostra a borda do disco sobre a África, onde o pixel tem dezenas de km e o
  ADP marca a poeira do Saara como fumaça: em 23/09/2026 14:50 UTC eram 303
  das 315 áreas, e sem o corte o painel do AmaView contava 89 mil km² de
  fumaça que ninguém via no mapa.
- **Leve.** O netCDF (~4 MB) é baixado para a memória, lido só nas linhas do
  setor NSA (o `Smoke` vem em blocos de 48 linhas) e descartado — **nenhum
  netCDF toca o disco**. Um quadro vira um GeoJSON de poucos KB a algumas
  dezenas (com gzip, bem menos) em ~0,3 s. 48 h ≈ 288 quadros, poucos MB.
- **48 h, no máximo.** Quadros mais velhos são apagados a cada rodada; o que
  falta na janela é preenchido do mais novo para o mais velho (até 36 por
  rodada, para o quadro novo nunca esperar; o que sobra fica na fila da
  próxima). Arquivo que falha espera 30 min antes de ser baixado de novo.
- **Cobertura.** Cada quadro diz em que fração da área a NOAA tentou detectar
  fumaça (`cob`, pelo ângulo solar do `PQI1`): de noite o `Smoke` vem **0**
  ("sem fumaça") no disco inteiro, e "sem fumaça" não é o mesmo que "sem dado".
- `amaview-fumaca.timer` a cada 2 min (a NOAA publica ~14 min depois da
  varredura). Cada olhada lista as duas horas mais novas no S3 (~5 KB cada);
  hora antiga com quadro faltando é relistada no máximo a cada 30 min.

| Peça | Onde |
|---|---|
| `fumaca/fumaca.py` | uma rodada: lista, baixa, contorna, indexa e apaga o velho |
| `fumaca/amaview-fumaca.{service,timer}` | usuário `amaview-fumaca`, `/var/cache/amaview-fumaca` |
| `fumaca/nginx-locais.conf` | `/fumaca/v2/`: CORS, gzip, quadro imutável, índice `no-cache` |

URLs: `/fumaca/v2/indice.json` (`{"gerado", "quadros": [{"c": "AAAADDDHHMM",
"n": áreas, "km2", "cob"}]}`) e `/fumaca/v2/quadros/{AAAADDDHHMM}.geojson`
(polígonos com `km2`). Logs: `journalctl -u amaview-fumaca`.

Testes (sem rede): `python3 -m unittest discover -s agendador/fumaca`.

## Qualidade do ar

A mesma máquina espelha as fontes de qualidade do ar para a camada
"Qualidade do ar" do AmaView. Nenhuma delas é chamada pelo navegador: a rede do
Acre não manda CORS utilizável, o Open-Meteo tem limite de uso por IP, as
redes de sensores só dão a leitura do momento (o espelho monta a série) e um
espelho só é mais robusto que vários servidores de terceiros.

- **Rede de Qualidade do Ar do Acre** (UFAC/MPAC,
  [acrequalidadedoar.ufac.br](https://acrequalidadedoar.ufac.br)): sensores
  PurpleAir com o PM2,5 corrigido pela própria rede. A API não tem série por
  sensor — só a leitura mais recente de cada um —, então **o espelho monta a
  série**: a cada 5 min guarda a leitura mais recente de cada sensor numa
  grade de 5 min, janela móvel de 48 h. A média horária por município que a
  rede calcula (`/readings/history`) vai junto: é o histórico que já existe
  quando o espelho começa, e o complemento quando um sensor falha. Em
  23/09/2026: 39 sensores no `latest-by-sensor`, 30 no cadastro, 14 vivos.
  Sensor fora do cadastro e parado há mais de 48 h (os de 2019–2022) sai;
  sensor do cadastro parado continua, sem série (o AmaView mostra "sem dado").
- **RedeAr** (IPAM/UFPA, [redear.org.br](https://redear.org.br), sem chave):
  `GET https://hmg.api.redear.org.br/v1/sensors` — 91 sensores do Brasil, cada
  um com as ~15 leituras mais recentes (~30 min; horário UTC sem "Z"): 82
  PurpleAir espelhados (`sensor_id` = `sensor_index` da PurpleAir; T em °F) e
  9 aparelhos próprios (T em °C). A **produção** (`api.redear.org.br`) parou
  em 21/07/2026; a homologação ("hmg") é a base, e a cada 6 h o espelho confere
  se a produção voltou a ter o dado mais novo (e troca). Só entram os
  sensores **dentro da Amazônia Legal pela coordenada** (o cadastro às vezes
  erra: o "Quilombo Mel da Pedreira", cadastrado como Belém/PA, fica em
  Macapá/AP). Os próprios da RedeAr têm série de 48 h na API
  (`/sensors/{id}/readings?startDate&endDate&limit=1000&offset`), usada para
  completar a do espelho a cada 6 h; os espelhados, não — o espelho acumula.
  Em 23/09/2026: 75 na Amazônia Legal, 59 vivos, 47 além dos da UFAC.
  O valor é o que o mapa da RedeAr mostra: a **média simples de A e B**
  ("env", a 0,1), sem correção; com o aviso "leituras divergentes" dela
  (|A − B| > 10 e ≥ 40% do maior) em `fl` = 4 — só aviso, nada é descartado.
- **AirGradient** (sem chave): a API do **mapa público**
  (`map-data.airgradient.com/map/api/v1`): 1×/dia os pontos no retângulo da
  Amazônia Legal (`/measurements/current/cluster?measure=pm25&zoom=18&…`, só
  `dataSource` AirGradient) e o cadastro de cada um (`/locations/{id}`: dono
  e licença); a cada rodada, a leitura (`/locations/{id}/measures/current`:
  `pm25` — o valor que o mapa mostra, com a correção da própria AirGradient —,
  `atmp`, `rhum`, `measuredAt`). 4 em 23/09/2026: Manaus, Lábrea e Porto
  Velho (Greenpeace) e Imperatriz.
- **PurpleAir** (opcional, chave em `/etc/amaview/purpleair-key`): 1×/dia a
  lista dos sensores externos no retângulo da Amazônia Legal
  (`/v1/sensors?fields=name,latitude,longitude&location_type=0&max_age=…&nwlng…`, mensal);
  a cada 120 min (`PURPLEAIR_INTERVALO_MIN`), todos menos os que a UFAC
  publica (leitura nas últimas 2 h) — os que a RedeAr repassa também, para
  saírem no valor e na escala da PurpleAir (`show_only=…&fields=pm2.5_10minute,channel_flags&max_age=600`
  — `pm2.5_10minute` é a média de 10 min de A e B da qual o mapa da PurpleAir
  calcula o AQI da EPA, sem conversão; com os dois canais degradados, sem valor). O saldo (`/v1/organization`, grátis) é
  lido antes e depois de cada consulta: o gasto real vai para `fontes`, e
  abaixo de 50.000 pontos (`PURPLEAIR_SALDO_MIN`) o espelho para de consultar.
- **OpenAQ v3** (opcional, chave em `/etc/amaview/openaq-key`): 1×/dia
  `/v3/locations?bbox=…`; a cada 60 min (`OPENAQ_INTERVALO_MIN`)
  `/v3/locations/{id}/latest` de cada local com MP2,5 (limites da OpenAQ: 60
  pedidos/min, 2.000/h). Local a menos de 200 m de um sensor de outra fonte é
  o mesmo aparelho repassado (a OpenAQ agrega AirGradient, PurpleAir…): fica a
  fonte direta.

  Sem o arquivo de chave, a fonte fica de fora em silêncio (`fontes.<nome>.nota`
  = "sem chave"). As chaves se instalam com `sudo amaview chave purpleair` /
  `sudo amaview chave openaq` (pedidas sem eco, testadas antes, gravadas 640
  root:amaview-ar). `/etc/amaview/ar.conf` (opcional, `CHAVE=valor`) muda os
  intervalos e o saldo mínimo.

  **Cada rede com o próprio dado** (desde 23/09/2026; antes o espelho punha
  todos na correção LRAPA da UFAC): o valor publicado é o da fonte, na
  unidade dela, sem correção do AmaView — UFAC `pm2_5_corrected`, RedeAr
  média de A e B, PurpleAir `pm2.5_10minute`, AirGradient `pm25` do mapa,
  OpenAQ `value`. A escala de cada rede (faixas e cores) é aplicada pelo
  AmaView. O estado guarda `valores` = 2; um estado de antes tem as séries
  corrigidas apagadas na primeira rodada, para não misturar réguas.
  **Um sensor, um ponto**: o mesmo `sensor_index` em várias fontes vira um
  item só, da fonte de maior prioridade (UFAC > PurpleAir > RedeAr) entre as
  que leram há pouco (2 h, ou a folga da fonte: 135 min na PurpleAir), com o valor e a série **só dela**; a leitura mais
  recente das outras vai em `outras` (`[{fonte, t, pm}]`) e o nome delas em
  `tambem`.
- **MonitorAr** (MMA, [monitorar.mma.gov.br](https://monitorar.mma.gov.br)):
  todas as estações oficiais da Amazônia Legal (os nove estados; o Maranhão a
  oeste de 44°W), com o **IQAr e a classificação CONAMA 491 que o próprio
  MonitorAr calcula**, por poluente. A API dá as últimas 24 medições
  horárias; o espelho acumula 48 h. `dtMedicao` vem **no horário de Brasília,
  sem fuso** (medido: às 16:09 BRT, o horário mais novo do país inteiro era
  16:00). Em 23/09/2026, só 2 das 12 estações da região tinham dado recente
  (Gapara e UTE Interna, em São Luís); uma tem `dtUltimaAtualizacao` em 2066,
  tratada como sem data.
- **CAMS global** (ECMWF/Copernicus) via [Open-Meteo](https://open-meteo.com/en/docs/air-quality-api):
  **modelo, não medição**. Grade de 0,8° sobre a Amazônia Legal (721
  células; os centros caem sobre a grade de 0,4° do CAMS, então cada valor é
  o de uma célula do modelo, sem interpolação): PM2,5, PM10, CO, O₃, NO₂ e
  profundidade óptica de aerossóis (AOD), horário. O modelo roda de 12 em
  12 h: o espelho lê o `meta.json` de hora em hora e só baixa a grade quando
  há rodada nova (ou a cada 12 h, por segurança) — ~1,5–3 mil chamadas por
  dia, em lotes de 100 pontos com 12 s entre eles (500/min, abaixo do limite
  de 600/min). Os lotes prontos ficam em `cams-bruto.json.parcial` e a rodada
  seguinte retoma os que faltam; um 429 faz esperar o limite zerar (minuto,
  hora ou dia) em vez de tentar a cada 5 min. Publica só as 48 h
  até a hora corrente: a previsão não entra na camada.

Nada é corrigido, filtrado nem reclassificado; os números só perdem o
resíduo de ponto flutuante (2 casas). A faixa de cada ponto é calculada no
AmaView, com a escala da rede de origem (`docs/qualidade-ar.md`).
Cada fonte roda isolada: se uma cai, as outras publicam, e `fontes` no
`pontos.json` diz quando cada uma respondeu pela última vez e o erro.

| Peça | Onde |
|---|---|
| `ar/ar.py` | uma rodada: lê as fontes, junta, acumula e publica |
| `ar/amazonia_legal.json` | contorno da Amazônia Legal (IBGE, simplificado a 0,1°) para a grade do modelo e o filtro dos sensores |
| `ar/municipios_al.json` | malha municipal da Amazônia Legal (IBGE, simplificada a ~600 m): município e UF de cada sensor pela coordenada |
| `ar/amostras/` | recortes de respostas reais (RedeAr, mapa da AirGradient, UFAC) para os testes |
| `ar/amaview-ar.{service,timer}` | a cada 5 min; usuário `amaview-ar`, `/var/cache/amaview-ar`; só a biblioteca padrão do Python |
| `ar/nginx-locais.conf` | `/ar/v1/`: CORS `*`, gzip, `max-age=60` |

URLs (em `https://147.15.84.134/ar/v1/`):

- `pontos.json` (~270 KB, ~13 KB com gzip): `sensores` (`t0`, `passoMin` 5,
  `slots` 577, `lista` com `id`, `cod`, `nome`, `mun`, `lat`, `lon`, `ult`
  `{t, pm, fl}` e `serie` — e, desde a RedeAr, `fonte` (`ufac`, `redear`,
  `purpleair`, `airgradient`, `openaq`), `uf`, `dono`, `ref` (id na origem),
  `pa` (aparelho PurpleAir), `ab` `{a, b, t, fl}` (canais "env"/"atm" da
  leitura mais recente da RedeAr), `met` `{temp, ur}` (medidas dentro do
  sensor), `tol` (min que a leitura vale no mapa, nas fontes horárias),
  `tambem` (outras fontes do mesmo sensor), `outras` (a leitura delas),
  `lic` (licença, AirGradient), `monitor` (OpenAQ: monitor de referência)), `municipios` (`h0`, `horas` 48, `serie`
  por município), `estacoes` (`h0`, `horas`, `lista` com `atual` e `polu`: por
  poluente, `iqar`, `cl` — id da classificação do MonitorAr — e `val`,
  validado) e `fontes` (`em`, `erro` e, nas com chave, `nota`, `saldo`,
  `gastoRodada`, `gastoDia`, `intervaloMin`). Ids: PurpleAir = `sensor_index`;
  próprios da RedeAr 900.000.000 + id; OpenAQ 920.000.000 + id; AirGradient
  (id do mapa) 3.000.000.000 + id. Tempos em epoch ms, UTC. Os campos novos são opcionais:
  o AmaView antigo lê o arquivo novo, e o novo lê o antigo.
- `cams.json` (~170 KB, ~50 KB com gzip): `celulas` `[lon, lat]`, `passo`,
  `h0`, `horas` e `pm2_5` por célula.
- `cams-series.json` (~1 MB, ~250 KB com gzip): as seis variáveis por célula
  (`vars`, `unidades`, `series`), para a ficha de uma célula.

Logs: `journalctl -u amaview-ar`. Testes (sem rede):
`python3 -m unittest discover -s agendador/ar`.

## Estações meteorológicas

A mesma máquina junta, para a camada "Estações meteorológicas" do AmaView, as
observações de superfície das últimas 48 h na Amazônia Legal:

- **INMET pelo WIS2 da OMM** (nó oficial `wis2bra.inmet.gov.br`, OGC API
  Features, sem token): estações automáticas (de hora em hora) e
  convencionais/sinóticas, já decodificadas do BUFR — uma feature por variável
  por estação. O navegador não lê direto: a resposta vem com
  `Access-Control-Allow-Origin` duplicado e o CORS quebra. Uma hora HH chega
  aos poucos até ~HH+60 min; a cada rodada as 4 horas mais novas têm a
  **contagem** conferida (`limit=1`, `numberMatched`, ~1 KB) e só são baixadas
  de novo quando ela muda; até 12 h, conferidas de hora em hora. O
  preenchimento inicial das 48 h leva ~1 min.
- **Chuva só de 1 h.** O período vem no `phenomenonTime`: as automáticas
  mandam 1 h (`HH-1/HH`); as convencionais, 12 e 24 h ou um instante sem
  período — esses ficam fora, porque não se somam à série horária.
- **METAR/SPECI dos aeródromos** (NOAA Aviation Weather Center): o
  `metars.cache.csv.gz` (~256 KB, mundo inteiro, atualizado a cada minuto)
  traz só a observação mais recente de cada aeródromo — **a série de 48 h é
  acumulada aqui**. A API (`/api/data/metar`, corta em 400 observações) é
  usada de hora em hora, em lotes de 6 aeródromos, para o preenchimento
  inicial, os buracos e os nomes. Nós → m/s, polegadas → hPa, visibilidade
  pelos 4 dígitos do próprio METAR (9999 e CAVOK = 10 km ou mais), umidade
  pela fórmula de Magnus. "WO ATTN" e afins (sem nenhuma medição) não entram.
- **Nomes.** Cadastro do INMET (`apitempo.inmet.gov.br/estacoes/T` e `/M`,
  exige User-Agent de navegador; uma vez por dia), casado pelo WIGOS
  (`CD_WSI`) e, nas convencionais, pelo índice da OMM. As sinóticas de
  aeroporto que o nó transmite mas que não estão no cadastro do INMET têm o
  nome lido uma vez do OSCAR/Surface da OMM — e saem quando há um METAR a
  menos de 5 km (mesmo lugar, dado repetido).
- **Recorte:** dentro da Amazônia Legal (IBGE, `meteo/amazonia_legal.json`,
  simplificado a ~3 km) ou a até 100 km da divisa — a faixa dá contexto na
  borda do mapa (Bolívia, Peru, Colômbia, Guianas, Goiás) sem trazer o Brasil
  inteiro.
- **Nada é inventado nem suavizado.** Hora sem observação fica `null`; valor
  fora da faixa física (ex.: orvalho de 99 °C) é defeito de sensor e fica
  fora; os números só perdem o resíduo de ponto flutuante (1 casa).

| Peça | Onde |
|---|---|
| `meteo/meteo.py` | uma rodada: confere/baixa as horas do WIS2, acumula o METAR, publica |
| `meteo/amazonia_legal.json` | contorno da Amazônia Legal (IBGE) para o recorte |
| `meteo/amaview-meteo.{service,timer}` | a cada 5 min; usuário `amaview-meteo`, `/var/cache/amaview-meteo`; só a biblioteca padrão do Python |
| `meteo/nginx-locais.conf` | `/meteo/v1/`: CORS `*`, gzip, `no-cache` (revalida com ETag) |

URLs (em `https://147.15.84.134/meteo/v1/`), tempos em epoch ms UTC:

- `estacoes.json` (~45 KB, ~10 KB com gzip): `estacoes`, cada uma com `k`
  (`i` INMET, `m` METAR), `c` (código: A101, 82332, SBMN), `w` (WIGOS),
  `n`, `uf`, `pais`, `x`/`y`/`z` e `u`, a última leitura (`ms`, `t`, `td`,
  `ur`, `vv`, `dv`, `raj`, `p`, `ps`, `chuva`, `chuva24`; METAR também `vis`,
  `wx`, `raw`).
- `serie.json` (~440 KB, ~90 KB com gzip): `t0`, `passoMin` 60, `slots` 48;
  `inmet` por código, uma lista de 48 valores por variável (`null` = sem
  observação); `metar` por ICAO, em colunas com os instantes próprios (`ms`,
  variáveis, `wx`, `raw`, `speci`).

Unidades: °C, %, m/s, graus (de onde o vento vem), hPa (`p` ao nível do mar;
`ps` na estação), mm na hora, metros.

Reserva não implementada: se o `wis2bra` cair, o Ogimet (`getsynop`) tem as
mesmas sinóticas em texto. Logs: `journalctl -u amaview-meteo`.

Testes (sem rede): `python3 -m unittest discover -s agendador/meteo`.

## Segurança

- O token fica em `/etc/amaview/token`, modo **600**, lido só pelo root.
- Nunca entra em argumento de linha de comando: argumentos de processo são
  visíveis para qualquer usuário da máquina.
- A permissão do PAT é a mínima que funciona — *Actions: Read and write* num
  repositório só. Esse token não lê código privado nem publica nada; ele só
  aperta este botão.
- Se vazar, revogue em <https://github.com/settings/tokens?type=beta>. O pior
  que alguém faz com ele é disparar ciclos de dados públicos.

