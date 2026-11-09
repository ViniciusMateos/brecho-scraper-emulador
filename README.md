# Brechó Scraper (emulador)

Raspagem do perfil do brechó (@brechoquasenadaa) pro app **Quase Nada Brechó**, tocando no app
real do Instagram dentro do emulador Android (MuMu Player) via `uiautomator2`. Acha peça nova, vê
qual vendeu ou mudou de preço e tira a foto de cada peça.

Faz parte do [quase-nada-bots-device](https://github.com/ViniciusMateos/quase-nada-bots-device):
fica em `bots/brecho_scraper/` e usa `emulador.py` da base e o bot `auto_follow` (dump, toques,
trocar conta).

## Por que existe

Até 21/10/2026 a raspagem rodava no servidor com Playwright, logada na conta da loja. O IG desafiou a
@brechoquasenadaa por logar de IP de datacenter. Agora quem lê é uma **conta auxiliar** no emulador,
olhando o perfil **público** da loja: a conta da loja nunca fica logada aqui (o módulo se recusa a
rodar nela).

## Quem faz o quê

- **Este módulo só coleta dados crus:** legenda em texto, shortcode do post e a foto da capa. Não
  interpreta nada.
- **O backend do brechó** interpreta tudo (peça ou divulgação, preço, `#p`, "vendido") com o mesmo
  parser de antes, casa com as peças e grava. Peça travada no app ("Manual") só ganha o link: se o
  post diz vendido, vira aviso pra conferir na mão, nunca venda automática.

Fluxo: app "Atualizar" → backend do brechó enfileira o job no backend dos Bots → o agente do PC
(`agente_emulador.py`) roda `run(d, params, log, parar)` numa conta auxiliar → o módulo manda os
posts pro backend do brechó.

## Como funciona

1. **Alvos:** pergunta ao backend o que ele já conhece (`GET /scraper/emulador/alvos`): peças
   disponíveis, posts recentes e peças sem link.
2. **Fase A (posts novos):** lê o feed do perfil do topo pra baixo, post a post, e para no primeiro
   post já conhecido. Se tem peça sem link (drop publicado), continua descendo, só lendo a legenda,
   até achar ou chegar na profundidade do drop mais antigo com peça sem link. O link vem de
   enviar → "Copiar ligação".
3. **Fase B (conferir as disponíveis):** abre cada peça disponível pelo link (`/p/<code>/`), lê a
   legenda e tira a foto. O log mostra o resultado ao vivo, peça a peça: **VENDIDA**,
   **DISPONÍVEL**, **ATUALIZADA · preço R$ x → R$ y**.
4. **Envio em lotes de 10** (`POST /scraper/emulador/posts`): o app atualiza durante a run, não só no
   fim.
5. **Parar** é imediato: o `parar()` do agente é checado entre os passos.

**Modo `fotos`** (`params.modo = "fotos"`): só recaptura a foto de toda peça com link, sem mexer em
nenhum outro dado. Foi usado em 26/10/2026 pra trocar as fotos antigas, que eram link de CDN do IG e
expiravam (394 de 394 em 115 min).

## Foto limpa

O print da capa sai **sem os overlays do IG**: a fileira de quem repostou (avatares, selo roxo, dica
"Partilha um emoji…"), o contador "1/3" do carrossel e os ícones de pessoas marcadas e de som.

- Os overlays são achados pelos nós do mesmo dump usado no recorte. Na faixa de baixo da mídia, onde
  mora a fileira de notas, o limite de tamanho é mais largo (3 avatares + dica passavam do limite
  antigo).
- Fundo liso em volta → pinta com a cor do fundo. O que **entra de fora** da caixa (alça, cadarço da
  peça) é preservado: só os nós de desenho (avatar, selo, dica, pílula do contador) são sempre
  apagados.
- Fundo que não é liso: se a maior parte é fundo, pinta com o fundo; se é tecido, faz um
  preenchimento suave puxando as beiradas. Nenhum balão fica.
- Corta ~10px da borda (cantos arredondados e o fio do card).
- As 3 primeiras fotos de cada run ficam em `debug/` (print inteiro, dump, foto limpa e o que a
  limpeza decidiu) pra calibrar. A pasta é ignorada no git: são telas do IG.

## Params do job

| Param | | |
|---|---|---|
| `brecho_base` | obrigatório | base do backend do brechó |
| `brecho_token` | obrigatório | token Bearer do backend do brechó |
| `run_id` | | id da run (vai no corpo do POST) |
| `full` | opcional | fase A lê todos os posts |
| `limite` | opcional | teto de posts da fase A (padrão 400) |
| `modo` | opcional | `"fotos"` = só recaptura as fotos |

`brecho_base` e `brecho_token` vêm do `agente_config.json` do PC, nunca do código.

## Armadilhas do IG no emulador

- Tocar no "… mais" de legenda de uma linha cai no `@` da conta e abre o **perfil**: o toque vai no
  lado direito e, se cair num grid, volta.
- "Estou num post?" = nenhuma célula do grid na tela + mídia, botão de enviar ou legenda.
- O grid carrega aos poucos (grid parado não é o fim) e voltar de um post pode deixá-lo em outra
  altura: a célula é procurada pros dois lados.
- O `#p` não é cronológico: drops intercalam números.

## Onde roda

```python
from bots.brecho_scraper import brecho_scraper as bs
res = bs.run(d, {"brecho_base": base, "brecho_token": token, "run_id": "run-1"}, log, parar)
```

O agente importa o módulo uma vez: depois de mexer no código, reinicie o agente (crie
`.restart_intencional` na raiz do device e mate o processo; o supervisor sobe de novo).
