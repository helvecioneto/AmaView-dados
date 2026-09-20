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
Universidade de Wyoming. Uma sondagem por estação: a mais recente das últimas
48 h.

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

### `cemaden/manifest.json`

Resumo do ciclo (instante, grandeza, cobertura, contagens). Pequeno — serve
para sondar se vale a pena rebaixar a série.

---

## Como funciona

```
cron */10
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
