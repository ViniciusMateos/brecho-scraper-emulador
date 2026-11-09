"""Raspagem do brechó pelo EMULADOR — adapta o scraper do brechó (antes Playwright server-side) pra
rodar NO DEVICE, tocando no app real do Instagram via uiautomator2.

Uma conta AUXILIAR do IG (NUNCA a conta da loja @brechoquasenadaa) abre o perfil PÚBLICO da loja no
app, lê os posts (legenda crua + shortcode + foto de capa recortada) e devolve tudo pro backend do
brechó por HTTP. Risco zero pra conta da loja: ela nem fica logada aqui.

Divisão de trabalho (IMPORTANTE):
  - o DEVICE só COLETA dados crus (legenda em texto, shortcode, imagem). NÃO parseia nada.
  - o BACKEND do brechó parseia (peça x divulgação, preço, vendida, #p…) reusando o MESMO parser do
    worker. Por isso aqui NÃO importamos nem chamamos parser.

Estratégia ESPERTA em 2 fases (modo COMPLETO, ~10-15 min — não desce o feed inteiro raspando tudo):
  INÍCIO: GET {brecho_base}/scraper/emulador/alvos → {"disponiveis":[{code,num}], "recentes":[codes]}.
  FASE A (posts NOVOS): lê o feed do TOPO, post a post, e PARA no 1º code/#p já conhecido (em
    `recentes`/`disponiveis`) — dali pra trás o app já tem tudo. full=true lê todos. Manda completos
    (code + legenda + imagem via SHARE→Copiar ligação→clipboard pro shortcode).
  FASE B (conferir VENDIDAS): pra cada `disponiveis`, abre /p/<code>/ pelo link (intent VIEW), lê a
    legenda + tira o print da capa (NÃO precisa do shortcode: o code já é conhecido). O backend
    detecta "vendido" na legenda e casa por code; o print migra a foto pra local permanente.

Contrato de saída (já implementado no backend do brechó — tem que bater EXATO):
  GET  {brecho_base}/scraper/emulador/alvos   (Authorization: Bearer {brecho_token})
  POST {brecho_base}/scraper/emulador/posts   (Authorization: Bearer {brecho_token})
  Body do POST: {"run_id": "...", "full": false, "done": true,
           "posts": [{"code": "<shortcode>", "taken_at": null,
                      "legenda": "<texto cru da legenda>", "imagem_b64": "<jpeg base64 da capa>"}]}
  `brecho_base`/`brecho_token` vêm nos PARAMS do job (não hardcodar). Envio em lotes (fase A + fase B
  juntas; done=false nos parciais, done=true só no último). O POST devolve stats
  {novas, atualizadas, recem_vendidas, reconciliadas, conferidas, total, imagens}.

Device: MuMu instância 0 (conta auxiliar, 127.0.0.1:16384 = emulador.SERIAL). O agente
(agente_emulador.py) cria o cliente uiautomator2 `d` e chama run(d, params, log, parar) — este
módulo RECEBE o `d`, nunca cria outro. Screencap é só pra imagem, nunca pra tocar.

Reuso: a navegação por resource-id/dump segue o modelo do bots/auto_follow (importado como `af`); o
padrão de abrir conteúdo por intent VIEW e o screencap+recorte seguem o bots/story_mumu.

IG 451 em pt-PT, tela 900x1600 layout TABLET (abas na barra lateral). Onde o resource-id real ainda
não é conhecido (o device não foi inspecionado nesta sessão), há fallback por texto/content-desc
(padrão do af._tocar_botao) e o id/texto fica marcado "A CONFIRMAR AO VIVO" no bloco de CONSTANTES
abaixo.
"""
import base64
import html
import io
import random
import re
import subprocess
import threading
import time

import emulador

from bots.auto_follow import auto_follow as af
from bots.auto_follow.auto_follow import _attr, _bounds, _dump, _log, _sleep, rid

# ════════════════════════════ CONSTANTES ════════════════════════════
PERFIL_LOJA = emulador.CONTA_BRECHO          # "brechoquasenadaa" — perfil PÚBLICO que vamos LER
LIMITE_PADRAO = 400                          # cap de segurança no nº de posts por run
LOTE_ENVIO = 15                              # posts por POST pro backend (done=false até o último)
MAX_SWIPES_SEM_MUDAR = 3                     # swipe no feed desiste depois de N tentativas sem andar
NAVEGACAO_PADRAO = "celula"                  # fase A: "celula" (tap→lê→back→próxima) | "auto" | "swipe"
N_RECENTES_FALLBACK = 40                     # só informativo; `recentes` vem do backend

# ── resource-ids / textos CONFIRMADOS no device real (IG 451 pt-PT, 900x1600, tablet, 21/10) ──
# Inspecionados ao vivo. Os fallbacks por geometria/texto continuam como rede de segurança caso a
# build do IG mude.

# GRID de posts do perfil: célula = android.widget.Button, resource-id "image_button"
# (content-desc tipo "6 fotos de quase nada na linha 1, coluna 1"; 1ª célula ~centro [205,937]).
# CONFIRMADO. Fallback = geometria (miniatura quadrada ~1/3 da largura abaixo do cabeçalho).
GRID_CELL_IDS = ("image_button",)
# posição (linha/coluna) da célula vem do content-desc — base da navegação célula-a-célula (fallback
# de navegação quando o swipe no feed não avança). CONFIRMADO o formato pt-PT ("linha L, coluna C").
_RE_GRID_POS = re.compile(r"linha\s*(\d+).*?coluna\s*(\d+)", re.I)
_RE_GRID_POS_EN = re.compile(r"row\s*(\d+).*?column\s*(\d+)", re.I)

# MÍDIA (foto de capa) dentro do post, pra recortar pelo bounds: resource-id "zoomable_view_container"
# (ou "carousel_image_media_group"/"carousel_image"), bounds típico [0,198]-[900,1323]. CONFIRMADO.
# Fallback = maior ImageView central na metade de cima da tela.
MEDIA_IDS = ("zoomable_view_container", "carousel_image_media_group", "carousel_image")

# SHARE (o caminho do shortcode): botão resource-id "row_feed_button_share" (content-desc "Enviar
# publicação…", centro ~[259,1387]) → abre a folha → "Copiar ligação". CONFIRMADO ao vivo (li
# DNmDCIHvLTC). NÃO usar o "…" (media_option_button = "Mais ações…") — NÃO é copiar link.
SHARE_IDS = ("row_feed_button_share",)
SHARE_DESC = ("Enviar publicação", "Enviar publicacao", "Send post", "Share post")  # fallback por desc
# na folha de enviar, o item que copia o link (pt-PT = "Copiar ligação"). CONFIRMADO.
COPIAR_LINK_TXT = ("Copiar ligação", "Copiar link", "Copy link", "Copiar hiperligação")

# LEGENDA: classe que TERMINA em "IgTextLayoutView" e SEM resource-id — o atributo `text` já é a
# legenda INTEIRA. CONFIRMADO. Não há id pra casar; casa-se por CLASSE + heurística (text com "|",
# "condi", "R$", "#p", "comente", "tam" ou terminando em "mais").
CAPTION_CLASS_SUFFIX = "IgTextLayoutView"
_CAPTION_PISTAS = ("|", "condi", "cond.", "r$", "#p", "comente", "tam", "mais", "…")
# legenda CORTADA (precisa expandir): o text termina em "mais"/"…". Tocar perto do fim do próprio nó
# (x1+60, y2-12) expande pro texto completo. CONFIRMADO ao vivo (abre #p/R$/hashtags).
MAIS_FIM = ("mais", "more", "…", "...")

# tela de conteúdo fora do IG / indisponível (IG joga pro Custom Tab/chromium ou mostra aviso).
INDISPONIVEL_TXT = ("conteúdo indisponível", "conteudo indisponivel",
                    "content isn't available", "content isnt available",
                    "esta página não está disponível", "esta pagina nao esta disponivel",
                    "page isn't available", "page isnt available")
# perfil privado / sem posts / parede de login — abortam o abrir_perfil com motivo.
PERFIL_RUIM_TXT = ("esta conta é privada", "esta conta e privada", "this account is private",
                   "conta privada", "ainda não há publicações", "ainda nao ha publicacoes",
                   "no posts yet", "sem publicações", "sem publicacoes",
                   "este perfil é privado", "este perfil e privado", "this profile is private")

_RE_SHORTCODE = re.compile(r"/p/([^/?#]+)")
_RE_NUM_PECA = re.compile(r"#p(\d+)\b", re.I)   # mesmo padrão do parser do backend


class ScraperAbort(Exception):
    """Aborta a run com um motivo (guarda de segurança, perfil inacessível, etc.)."""


# ════════════════════════════ adb / screencap (padrão do story_mumu) ════════════════════════════
# Replicado (não importado) do story_mumu: cada módulo tem a SUA thread-local de serial, pra o
# screencap do brechó não depender do estado interno de outro módulo. Default = emulador.SERIAL
# (instância 0, onde roda a conta auxiliar). O screencap serve SÓ pra foto — nunca pra tocar.
_local = threading.local()

# Windows: esconde a janela de console do adb. Sem isto, CADA screencap (1x por peça na fase B)
# PISCA um cmd na cara do usuário. CREATE_NO_WINDOW só existe no Windows → 0 (no-op) no resto.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _serial():
    return getattr(_local, "serial", None) or emulador.SERIAL


def usar_serial(serial):
    """Aponta o adb (screencap) DESTA thread pra outra instância. Chame de volta com emulador.SERIAL."""
    _local.serial = serial


def _adb_bytes(*args, timeout=60):
    """Saída BRUTA de um comando adb na instância desta thread (ex.: screencap -p). creationflags
    esconde a janela de cmd no Windows (senão pisca a cada screencap)."""
    return subprocess.run([emulador.ADB, "-s", _serial(), *args],
                          capture_output=True, timeout=timeout, creationflags=_NO_WINDOW).stdout


def _print_tela(d=None):
    """Captura a tela inteira como PIL.Image (RGB). Tenta adb screencap (padrão do story_mumu) e,
    se falhar, cai pro screenshot do próprio uiautomator2 (que já aponta pro device certo)."""
    from PIL import Image
    try:
        raw = _adb_bytes("exec-out", "screencap", "-p")
        if raw:
            return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        pass
    if d is not None:
        try:
            return d.screenshot().convert("RGB")   # fallback robusto do uiautomator2
        except Exception:
            pass
    return None


# ════════════════════════════ helpers de leitura ════════════════════════════
def _nodes(xml):
    """Itera (tag_bruta, bounds) de cada node do dump (bounds válido)."""
    for m in re.finditer(r"<node[^>]*/?>", xml or ""):
        t = m.group(0)
        bb = _bounds(t)
        if bb:
            yield t, bb


def _tela_tem(d, frases):
    """Alguma das `frases` (minúsculas) aparece no dump? (texto ou content-desc)."""
    low = (_dump(d) or "").lower()
    return any(f in low for f in frases)


def _no_ig(d):
    """O IG ainda está em primeiro plano? (False = IG abriu Custom Tab/chromium, outro pacote)."""
    try:
        return (d.app_current() or {}).get("package") == af.IG_PKG
    except Exception:
        return True   # na dúvida, não derruba a run


def _por_id(d, ids):
    """Primeiro node existente entre os resource-ids candidatos (lista 'A CONFIRMAR'). Devolve o
    objeto uiautomator2 ou None."""
    for i in ids:
        el = d(resourceId=rid(i))
        try:
            if el.exists:
                return el
        except Exception:
            pass
    return None


def _bounds_por_id(d, ids):
    """Bounds (x1,y1,x2,y2) do primeiro resource-id candidato que existir, ou None."""
    xml = _dump(d)
    alvo = {rid(i) for i in ids}
    for t, bb in _nodes(xml):
        if _attr(t, "resource-id") in alvo:
            return bb
    return None


# ════════════════════════════ guarda de segurança ════════════════════════════
def conta_segura(d, log=None):
    """GUARDA OBRIGATÓRIA: confirma que a conta LOGADA (ativa) NÃO é a da loja antes de raspar.

    Vai pro perfil (af.garantir_perfil reusa a navegação/retentativa do auto_follow) e lê a conta
    ativa com af.conta_atual. Se for emulador.CONTA_BRECHO → aborta (NUNCA raspar logado na loja).
    Se não der pra confirmar quem está logado → também aborta (não dá pra garantir que não é a loja).
    Devolve o @ da conta auxiliar confirmada."""
    try:
        af.garantir_perfil(d, log)
    except Exception:
        pass
    atual = (af.conta_atual(d) or "").strip().lstrip("@").lower()
    if not atual:
        raise ScraperAbort("não consegui confirmar a conta ativa — abortando por segurança "
                           "(não raspo sem ter certeza de que não é a conta da loja)")
    if atual == str(PERFIL_LOJA).strip().lstrip("@").lower():
        raise ScraperAbort(f"a conta ATIVA é a da loja (@{PERFIL_LOJA}) — NUNCA raspar logado na "
                           f"loja. Troque pra uma conta auxiliar antes de rodar.")
    _log(log, f"Conta ··· @{atual}")
    return atual


# ════════════════════════════ navegação do perfil/feed ════════════════════════════
def abrir_perfil(d, username=PERFIL_LOJA, log=None):
    """Abre o perfil público `username` por intent VIEW (padrão do story_mumu.abrir_repost) e espera
    o grid de posts. Aborta (ScraperAbort) se cair em login/challenge/privado/indisponível."""
    af.garantir_app(d, log)
    url = f"https://www.instagram.com/{username}/"
    try:
        d.shell(f"am start -a android.intent.action.VIEW -d {url} -p {af.IG_PKG}")
    except Exception as e:
        raise ScraperAbort(f"não consegui abrir o perfil por intent: {e}")
    _sleep(3.0, 4.5)
    # espera o perfil assentar: o cabeçalho com o @username OU o grid de miniaturas
    for _ in range(12):
        af.fechar_permissao_android(d, log)
        if not _no_ig(d):
            d.press("back")                       # IG jogou pra fora (Custom Tab) — volta
            _sleep(1.2, 2.0)
            continue
        if _tela_tem(d, PERFIL_RUIM_TXT):
            raise ScraperAbort(f"perfil @{username} privado/sem posts — nada a raspar")
        if _tela_tem(d, INDISPONIVEL_TXT):
            raise ScraperAbort(f"perfil @{username} indisponível")
        if af.tela_bloqueio(d) or af._tem_form_login(_dump(d)):
            raise ScraperAbort("caiu em login/challenge ao abrir o perfil — conta auxiliar precisa "
                               "de atenção")
        if _primeira_celula_grid(d) is not None:
            return True
        _sleep(1.2, 2.0)
    raise ScraperAbort(f"o grid de @{username} não carregou")


def _primeira_celula_grid(d):
    """Bounds da 1ª miniatura (a mais NOVA, canto superior-esquerdo) do grid do perfil. Tenta por
    resource-id (A CONFIRMAR) e cai pra geometria: miniaturas quadradas, lado ~1/3 da largura, abaixo
    do cabeçalho do perfil. Devolve bounds ou None."""
    bb = _bounds_por_id(d, GRID_CELL_IDS)
    if bb:
        return bb
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    cands = []
    for t, (x1, y1, x2, y2) in _nodes(_dump(d)):
        cls = _attr(t, "class")
        if "ImageView" not in cls and "Button" not in cls:
            continue
        w, h = x2 - x1, y2 - y1
        if w < W * 0.22 or w > W * 0.40:          # célula do grid ~1/3 da largura
            continue
        if abs(w - h) > max(14, w * 0.25):        # quase quadrada
            continue
        if y1 < H * 0.18:                         # abaixo do cabeçalho/foto do perfil
            continue
        cands.append((x1, y1, x2, y2))
    if not cands:
        return None
    # a 1ª célula = a mais acima e, dentre as do topo, a mais à esquerda
    cands.sort(key=lambda b: (b[1] // 20, b[0]))
    return cands[0]


def entrar_no_feed(d, log=None, tentativas=3):
    """Toca a 1ª célula do grid → abre o feed de DETALHE vertical (do post mais novo pro mais
    antigo). Confere que entrou MESMO num post (_no_post) e tenta de novo se o toque se perdeu."""
    for tent in range(tentativas):
        bb = _primeira_celula_grid(d)
        if not bb:
            raise ScraperAbort("não achei a 1ª miniatura do grid pra entrar no feed")
        _sleep(0.5, 0.8)                     # grid parado antes do toque
        d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
        for _ in range(10):
            _sleep(0.8, 1.3)
            if _no_post(d):
                return True
        if not _no_grid(d):                  # abriu outra coisa → volta pro grid antes de tentar de novo
            _voltar_ao_grid(d, log)
    raise ScraperAbort("não entrei no feed de detalhe ao tocar a miniatura")


FEED_TITULOS = ("Publicações", "Publicacoes", "Posts")   # título do topo do feed de detalhe


def _no_post(d):
    """Estamos DENTRO de um post aberto (feed de detalhe) — e NÃO no perfil? Negativo: célula do grid
    na tela (= perfil). Positivo: a mídia do post por resource-id, o botão de enviar, ou o título
    'Publicações'/'Posts' no topo. NÃO usa a geometria de 'mídia grande' (o perfil também passava
    nela — foi assim que o bot leu a página do perfil como se fosse a legenda de um post)."""
    xml = _dump(d) or ""
    try:
        _W, H = d.window_size()
    except Exception:
        H = 1600
    ids_grid = {rid(i) for i in GRID_CELL_IDS}
    ids_midia = {rid(i) for i in MEDIA_IDS}
    ids_share = {rid(i) for i in SHARE_IDS}
    positivo = False
    for t, bb in _nodes(xml):
        r = _attr(t, "resource-id")
        desc = _attr(t, "content-desc")
        if r in ids_grid or _RE_GRID_POS.search(desc) or _RE_GRID_POS_EN.search(desc):
            return False
        if (r in ids_midia or r in ids_share
                or any(desc.strip().lower().startswith(s.lower()) for s in SHARE_DESC)
                or (_attr(t, "text").strip() in FEED_TITULOS and bb[1] < H * 0.15)
                or (_attr(t, "class").endswith(CAPTION_CLASS_SUFFIX)          # nó da legenda: "@loja | …"
                    and str(PERFIL_LOJA).lower() in _attr(t, "text").lower())):
            positivo = True
    return positivo


def _em_feed_detalhe(d):
    """Compat: estamos num post aberto (não no perfil)? → _no_post."""
    return _no_post(d)


# ════════════════════════════ leitura de UM post ════════════════════════════
def _bounds_midia(d, xml=None):
    """Bounds da mídia (foto de capa) do post atual. Por id (A CONFIRMAR) e fallback geométrico: o
    maior ImageView central na metade de cima da tela. `xml` = dump já tirado (o recorte da capa usa
    o MESMO dump pra achar a mídia e os overlays em cima dela); sem ele, tira um agora."""
    if xml is None:
        xml = _dump(d)
    alvo = {rid(i) for i in MEDIA_IDS}
    for t, bb in _nodes(xml):
        if _attr(t, "resource-id") in alvo:
            return bb
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    melhor = None
    for t, (x1, y1, x2, y2) in _nodes(xml):
        if "ImageView" not in _attr(t, "class") and "View" != _attr(t, "class"):
            continue
        w, h = x2 - x1, y2 - y1
        if w < W * 0.6 or h < H * 0.25:           # mídia ocupa quase a largura e boa altura
            continue
        if y1 > H * 0.7:                          # na metade de cima (foge do rodapé/legenda)
            continue
        area = w * h
        if melhor is None or area > melhor[1]:
            melhor = ((x1, y1, x2, y2), area)
    return melhor[0] if melhor else None


def _bounds_share(d):
    """Bounds do botão SHARE (row_feed_button_share) do post em foco. Por id (CONFIRMADO) e fallback
    por content-desc ('Enviar publicação…'). Devolve o mais acima (o do post em foco)."""
    bb = _bounds_por_id(d, SHARE_IDS)
    if bb:
        return bb
    cands = []
    for t, b in _nodes(_dump(d)):
        desc = _attr(t, "content-desc").strip().lower()
        if any(desc.startswith(s.lower()) for s in SHARE_DESC):
            cands.append(b)
    return min(cands, key=lambda b: b[1]) if cands else None


def _bounds_caption(d):
    """Bounds + texto da legenda. CONFIRMADO: a legenda é um nó de classe que TERMINA em
    "IgTextLayoutView" (sem resource-id) cujo atributo `text` já é a legenda INTEIRA. Casa-se por
    CLASSE + heurística (text com alguma pista de legenda de peça, ou o maior bloco abaixo da mídia).
    Fallback final = TextView com mais texto abaixo da mídia. Devolve (bounds, texto) ou (None, '')."""
    xml = _dump(d)
    try:
        _W, H = d.window_size()
    except Exception:
        H = 1600
    # 1) nós IgTextLayoutView (a legenda confirmada). A legenda de verdade COMEÇA com o @ da loja
    #    ("brechoquasenadaa | …"): se tem algum assim, pega o MAIS DE CIMA (o post em foco — o de baixo,
    #    se aparecer, é o próximo post). Senão: o que bate uma pista de peça, depois o mais longo.
    cand_classe = []
    da_loja = []
    loja = str(PERFIL_LOJA).lower()
    for t, bb in _nodes(xml):
        if not _attr(t, "class").endswith(CAPTION_CLASS_SUFFIX):
            continue
        txt = _attr(t, "text")
        if len(txt) < 4:
            continue
        if html.unescape(txt).lstrip().lower().startswith(loja):
            da_loja.append((bb[1], bb, txt))
        pista = any(p in txt.lower() for p in _CAPTION_PISTAS)
        cand_classe.append((pista, len(txt), bb, txt))
    if da_loja:
        da_loja.sort(key=lambda c: c[0])
        _y, bb, txt = da_loja[0]
        return bb, txt
    if cand_classe:
        cand_classe.sort(key=lambda c: (c[0], c[1]), reverse=True)  # com pista e mais longo primeiro
        _p, _n, bb, txt = cand_classe[0]
        return bb, txt
    # 2) fallback: maior TextView abaixo da mídia (build diferente / a11y mudou)
    melhor = None
    for t, bb in _nodes(xml):
        if "TextView" not in _attr(t, "class"):
            continue
        txt = _attr(t, "text")
        if len(txt) < 12 or bb[1] < H * 0.3:
            continue
        if melhor is None or len(txt) > len(melhor[1]):
            melhor = (bb, txt)
    return melhor if melhor else (None, "")


def _legenda_limpa(txt):
    """Normaliza a legenda CRUA lida do nó IgTextLayoutView ANTES de mandar pro backend. FONTE ÚNICA
    (chamada nas DUAS fases). Corrige o que quebrou a run-407 (legenda inteira no campo nome de 80
    peças):
      1) DECODIFICA entidades XML do dump (&#10;->\\n, &amp;->&, …): sem isso as quebras de linha vêm
         como "&#10;" e o parser do backend não acha as linhas → o nome vira a legenda toda;
      2) TIRA o prefixo do usuário "brechoquasenadaa | " (às vezes "@brechoquasenadaa | ") do começo;
      3) TIRA o "… mais"/"menos"/"more"/"less" que sobra no fim do nó.
    Segura com None/vazio e idempotente."""
    txt = html.unescape(txt or "")
    txt = re.sub(r"^\s*@?brechoquasenadaa\s*\|\s*", "", txt, flags=re.I)
    txt = re.sub(r"\s*(?:…\s*)?(?:mais|menos|more|less)\s*$", "", txt, flags=re.I).rstrip()
    return txt


def _legenda_cortada(txt):
    low = (txt or "").rstrip().lower()
    return low.endswith(MAIS_FIM) or ("… mais" in low) or ("...mais" in low)


def expandir_legenda(d, log=None):
    """Expande a legenda cortada tocando no TEXTO dela ('… mais' é um span dentro do próprio nó
    IgTextLayoutView). CUIDADO com ONDE tocar: a legenda começa com o @ da loja, que é LINK. Numa
    legenda que aparece em UMA linha (ex.: peça vendida "brechoquasenadaa | ❌VENDIDO❌ … mais") o ponto
    antigo (x1+60) caía em cima do @ e ABRIA O PERFIL — e o bot lia o perfil como se fosse a legenda
    (sem #p, sem 'vendido'). Agora: legenda de várias linhas → começo da ÚLTIMA linha (texto comum,
    ponto confirmado ao vivo); de uma linha → lado direito, onde fica o '… mais'. Depois de cada toque,
    se caiu num grid (perfil/hashtag), volta (back) e tenta o próximo ponto. True se tocou."""
    bb, txt = _bounds_caption(d)
    if not bb or not _legenda_cortada(txt):
        return False
    x1, y1, x2, y2 = bb
    uma_linha = (y2 - y1) < 40               # linha ~27px no tablet; errar pra "várias" é seguro (o back desfaz)
    fracs = ([] if uma_linha else [None]) + [0.85, 0.6]
    for f in fracs:
        x = x1 + 60 if f is None else int(x1 + (x2 - x1) * f)
        d.click(int(x), int(y2 - 12))
        _sleep(0.6, 1.1)
        if _no_grid(d):                      # o toque navegou (perfil/hashtag): volta pro post
            d.press("back")
            _sleep(0.8, 1.2)
            if _no_grid(d):
                return True                  # não voltou pro post — quem chamou confere
            continue
        nb, ntxt = _bounds_caption(d)
        if not nb or not _legenda_cortada(ntxt):
            return True                      # expandiu
        x1, y1, x2, y2 = nb
    return True


def _copiar_shortcode(d, log=None):
    """Pega o shortcode do post pelo caminho CONFIRMADO: botão SHARE (row_feed_button_share) → folha
    de enviar → 'Copiar ligação' → lê o clipboard → regex /p/<code>. Limpa o clipboard antes. Devolve
    o code ou None (sem derrubar a run). (O '…'/media_option_button é 'Mais ações…', NÃO copia link.)"""
    try:
        d.set_clipboard("")
    except Exception:
        pass
    # CORREÇÃO (teste ao vivo): no tablet o row_feed_button_share às vezes fica ABAIXO da dobra →
    # rola ~300px pra cima na área da mídia e procura de novo (1-2 vezes) antes de desistir.
    sh = _bounds_share(d)
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    tent = 0
    while not sh and tent < 2:
        mid = _bounds_midia(d) or (0, int(H * 0.25), W, int(H * 0.8))
        cx = (mid[0] + mid[2]) // 2
        y1 = (mid[1] + mid[3]) // 2
        d.swipe(cx, y1, cx, max(0, y1 - 300), 0.2)   # conteúdo sobe → revela o botão de baixo
        _sleep(0.8, 1.3)
        sh = _bounds_share(d)
        tent += 1
    if not sh:
        _log(log, "  (não consegui pegar o link desse post — sem botão de enviar)")
        return None
    d.click((sh[0] + sh[2]) // 2, (sh[1] + sh[3]) // 2)
    _sleep(0.9, 1.4)
    # na folha, toca "Copiar ligação"/"Copiar link" (reusa o fallback por texto/desc do af)
    if not af._tocar_botao(d, list(COPIAR_LINK_TXT), exato=True):
        if not af._tocar_botao(d, list(COPIAR_LINK_TXT), exato=False):
            _log(log, "  (não consegui pegar o link desse post — sem 'Copiar ligação')")
            d.press("back")
            return None
    _sleep(0.8, 1.3)
    code = None
    try:
        m = _RE_SHORTCODE.search(d.clipboard or "")
        code = m.group(1) if m else None
    except Exception:
        code = None
    # a folha de enviar costuma fechar sozinha ao copiar; se ficou aberta (ainda sem o botão SHARE do
    # post em foco), fecha com back
    if _bounds_share(d) is None:
        d.press("back")
        _sleep(0.5, 0.9)
    return code


# ════════════════════════════ limpeza da foto (overlays do IG por cima da mídia) ════════════════
# O IG desenha coisas EM CIMA da foto do post: o facepile do repost (avatar redondo + selinho roxo,
# canto inferior esquerdo), o contador do carrossel ("1/4", canto superior direito), o ícone de
# pessoas marcadas, o de som… O print pega tudo junto e a peça ficava com o avatar de alguém na foto.
# A limpeza pinta cada overlay com a cor do FUNDO estimada num anel fino em volta dele (mediana) —
# quando esse anel é liso (o fundo branco/claro da foto de produto). Anel não liso (sombra, degradê,
# a própria roupa) → preenche "puxando" as beiradas pra dentro (tipo inpaint): fica uma mancha suave
# no lugar, mas NENHUM balão sobra (Vinicius, 09/11: "é pra todos não terem esse balão").
#   1ª passada: pelos NÓS do dump (o MESMO dump do recorte) — nó pequeno DENTRO da mídia = overlay.
#               Na faixa de BAIXO da mídia mora a fileira de notas/repost (avatar + selo roxo + balão
#               de dica "Partilha um emoji…"): com 3 avatares (ou 2 + dica) ela passa do limite de
#               tamanho — lá vale um limite próprio, mais largo, e os vizinhos viram um retângulo só.
#   2ª passada: pelos PIXELS nos cantos onde o IG põe overlay — pega o que não aparece no dump: uma
#               mancha compacta e ISOLADA (não encosta na borda da região) num canto de fundo liso.
#               A peça nunca cabe inteira num canto: se ela chega ali, encosta na borda → não mexe.
# Tudo best-effort: qualquer falha devolve o recorte cru (o comportamento antigo).
OVERLAY_MAX_LADO = 0.34       # lado do overlay <= 34% do lado da mídia (maior = conteúdo/contêiner)
OVERLAY_MAX_AREA = 0.06       # área <= 6% da área da mídia
OVERLAY_MIN_LADO = 6          # px de tela: nó degenerado/minúsculo é ignorado
OVERLAY_JUNTA = 8             # px de tela: nós a essa distância viram UM overlay (avatar + selinho)
OVERLAY_FOLGAS = ((6, 0.15), (8, 0.0))   # folga em volta (px mín, fração do lado): a larga, depois a justa
#                                          (justa >= 8: o círculo do avatar e a sombra vazam ~6px do nó)
OVERLAY_DENTRO_MIN = 0.5      # nó que VAZA da mídia conta se >= 50% dele está dentro (corta na borda)
OVERLAY_FAIXA_BAIXO = 0.60    # faixa de baixo = a partir de 60% da altura da mídia (fileira de notas)
OVERLAY_BAIXO_LARG = 0.80     # lá a fileira pode ocupar até 80% da largura (3+ avatares + dica)
OVERLAY_BAIXO_ALT = 0.30      # … e até 30% da altura
OVERLAY_JUNTA_BAIXO = 40      # px de tela: lá, avatares vizinhos (gap de ~10-30px) viram UM retângulo
OVERLAY_ENCOSTA_BAIXO = 48    # px do recorte: grupo da faixa de baixo a essa distância do pé da mídia
#                               é esticado até o pé (o círculo/sombra passa do nó e sobrava um toco)
ANEL_PX = 10                  # espessura (px do print) do anel que estima a cor do fundo
ANEL_TOL = 22                 # diferença máx (0-255, no canal mais diferente) de um px do anel pra mediana
ANEL_MIN_LISO = 0.96          # fração mínima do anel dentro da tolerância pra ser "fundo liso"
ANEL_MIN_MAIORIA = 0.60       # anel não liso mas com >= 60% de fundo (ex.: cadarço/alça da peça
#                               cruzando o fundo branco) → pinta com o fundo mesmo (o pedaço da alça
#                               que entra na caixa some; o inpaint aí puxava a alça e virava borrão)
LIMPAR_CANTOS = True          # liga a 2ª passada (pixels nos cantos)
CANTOS = ("inf_esq", "sup_dir")   # facepile/etiquetas (inferior esquerdo) e contador (superior direito)
# região do canto = quadrado com essa fração do menor lado do recorte; tenta da MENOR pra maior (a
# peça às vezes chega perto do canto — região justa isola o overlay sem pegar a roupa)
CANTO_LADOS = (0.14, 0.20, 0.26)
CANTO_BORDA = 8               # px: faixa na borda da região que tem que ser fundo liso
CANTO_TOL = 30                # px "diferente do fundo" = algum canal a mais de 30 da mediana
CANTO_DENSIDADE = 0.35        # mancha compacta (selo/avatar), não pontinhos espalhados


def _agrupar_rects(rects, junta):
    """Junta retângulos que se tocam (ou ficam a até `junta` px) num só — o facepile vem como
    contêiner + avatar + selinho; o anel tem que ficar em volta do CONJUNTO."""
    rs = [list(r) for r in rects]
    mudou = True
    while mudou:
        mudou = False
        out = []
        while rs:
            a = rs.pop()
            i = 0
            while i < len(rs):
                b = rs[i]
                if (a[0] - junta <= b[2] and b[0] - junta <= a[2]
                        and a[1] - junta <= b[3] and b[1] - junta <= a[3]):
                    a = [min(a[0], b[0]), min(a[1], b[1]), max(a[2], b[2]), max(a[3], b[3])]
                    rs.pop(i)
                    mudou = True
                else:
                    i += 1
            out.append(a)
        rs = out
    return [tuple(r) for r in rs]


CLASSES_DESENHO = ("ImageView", "TextView", "Button", "ImageButton")


def _folhas(grupo, rects, desenhos=()):
    """Os nós de um grupo marcados como DESENHO ou contêiner. Desenho = não contém outro nó (selo,
    balão de dica) ou é imagem/texto (`desenhos` — o avatar é ImageView com o selo DENTRO). Contêiner
    (a nota inteira, a fileira) é mais alto que o desenho, e a alça da peça que passa no vazio dele
    tem que ser preservada.
    Devolve TODOS os nós do grupo como (x1, y1, x2, y2, fixo): fixo=1 = desenho (núcleo sempre);
    fixo=0 = contêiner — só vira núcleo se na foto ele é uma forma CHEIA (a pílula do contador
    "1/3", o círculo do ícone de marcados), o que o _pintar_preservando mede nos pixels."""
    gx1, gy1, gx2, gy2 = grupo
    dentro = [r for r in set(rects) if gx1 <= r[0] and gy1 <= r[1] and r[2] <= gx2 and r[3] <= gy2]
    return [tuple(r) + (1 if (r in desenhos or not any(
                o != r and r[0] <= o[0] and r[1] <= o[1] and o[2] <= r[2] and o[3] <= r[3]
                for o in dentro)) else 0,)
            for r in dentro]


def _overlays_na_midia(xml, midia, com_nucleos=False):
    """Retângulos (coordenadas de TELA) dos nós desenhados em cima da mídia: nó DENTRO das bounds da
    mídia (o que vaza um pouco da borda é cortado nela; só conta se >= OVERLAY_DENTRO_MIN dele está
    dentro), pequeno perto dela (lado <= OVERLAY_MAX_LADO, área <= OVERLAY_MAX_AREA) e que não é a
    própria mídia/contêiner. Nós colados viram um retângulo só. Grupo grande demais é descartado —
    menos na faixa de baixo (a fileira de notas/repost), que tem limite próprio mais largo.
    `com_nucleos` → [(grupo, [nós-folha do grupo])] (o núcleo é o que é pintado SEMPRE; fora dele a
    limpeza preserva o que entra de fora da caixa, tipo a alça da peça)."""
    mx1, my1, mx2, my2 = midia
    mw, mh = mx2 - mx1, my2 - my1
    if mw <= 0 or mh <= 0:
        return []
    ids_midia = {rid(i) for i in MEDIA_IDS}
    y_baixo = my1 + mh * OVERLAY_FAIXA_BAIXO

    def _cabe(x1, y1, x2, y2):
        w, h = x2 - x1, y2 - y1
        return (w >= OVERLAY_MIN_LADO and h >= OVERLAY_MIN_LADO
                and w <= mw * OVERLAY_MAX_LADO and h <= mh * OVERLAY_MAX_LADO
                and w * h <= mw * mh * OVERLAY_MAX_AREA)

    def _cabe_baixo(x1, y1, x2, y2):
        w, h = x2 - x1, y2 - y1
        return (y1 >= y_baixo and w >= OVERLAY_MIN_LADO and h >= OVERLAY_MIN_LADO
                and w <= mw * OVERLAY_BAIXO_LARG and h <= mh * OVERLAY_BAIXO_ALT)

    rects, desenhos = [], set()
    for t, (x1, y1, x2, y2) in _nodes(xml):
        if (x1, y1, x2, y2) == tuple(midia) or _attr(t, "resource-id") in ids_midia:
            continue
        area = (x2 - x1) * (y2 - y1)
        ix1, iy1, ix2, iy2 = max(x1, mx1), max(y1, my1), min(x2, mx2), min(y2, my2)
        if area <= 0 or ix2 <= ix1 or iy2 <= iy1:
            continue
        if (ix2 - ix1) * (iy2 - iy1) < area * OVERLAY_DENTRO_MIN:
            continue                              # mais fora do que dentro = não é overlay da mídia
        if _cabe(ix1, iy1, ix2, iy2):
            rects.append((ix1, iy1, ix2, iy2))
            if (_attr(t, "class") or "").endswith(CLASSES_DESENHO):
                desenhos.add((ix1, iy1, ix2, iy2))
    cima = [r for r in rects if r[1] < y_baixo]
    baixo = [r for r in rects if r[1] >= y_baixo]
    out = [(r, cima) for r in _agrupar_rects(cima, OVERLAY_JUNTA) if _cabe(*r)]
    out += [(r, baixo) for r in _agrupar_rects(baixo, OVERLAY_JUNTA_BAIXO)
            if _cabe(*r) or _cabe_baixo(*r)]
    if not com_nucleos:
        return [r for r, _ in out]
    return [(r, _folhas(r, membros, desenhos)) for r, membros in out]


def _dif_max(pedaco, cor):
    """Imagem L com a MAIOR diferença por canal entre cada px de `pedaco` (RGB) e a `cor`."""
    from PIL import Image, ImageChops
    r, g, b = ImageChops.difference(pedaco, Image.new("RGB", pedaco.size, cor)).split()
    return ImageChops.lighter(ImageChops.lighter(r, g), b)


def _fundo_liso(pedaco, mascara, minimo=ANEL_MIN_LISO):
    """Cor do fundo (mediana por canal) dos px de `pedaco` onde `mascara` (L) = 255 — SÓ se essa área
    é lisa (>= `minimo` dos px a até ANEL_TOL da mediana). Devolve (r, g, b) ou None."""
    from PIL import ImageChops, ImageStat
    n = mascara.histogram()[255]
    if n < 40:
        return None                               # quase nada de referência (anel fora do recorte)
    cor = tuple(int(round(v)) for v in ImageStat.Stat(pedaco, mascara).median)
    perto = _dif_max(pedaco, cor).point(lambda v: 255 if v <= ANEL_TOL else 0)
    n_ok = ImageChops.multiply(perto, mascara).histogram()[255]
    return cor if n_ok >= n * minimo else None


def _cor_do_anel(img, caixa, minimo=ANEL_MIN_LISO):
    """Cor do fundo no anel em volta de `caixa` (x1, y1, x2, y2 no recorte, fim exclusivo) — só se o
    anel é liso (>= `minimo` dele é a mesma cor). (r, g, b) ou None."""
    from PIL import Image, ImageDraw
    W, H = img.size
    x1, y1 = max(0, int(caixa[0])), max(0, int(caixa[1]))
    x2, y2 = min(W, int(caixa[2])), min(H, int(caixa[3]))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return None
    a = ANEL_PX
    ax1, ay1, ax2, ay2 = max(0, x1 - a), max(0, y1 - a), min(W, x2 + a), min(H, y2 + a)
    pedaco = img.crop((ax1, ay1, ax2, ay2))
    anel = Image.new("L", pedaco.size, 255)
    ImageDraw.Draw(anel).rectangle((x1 - ax1, y1 - ay1, x2 - ax1 - 1, y2 - ay1 - 1), fill=0)
    return _fundo_liso(pedaco, anel, minimo)


def _pintar_se_liso(img, caixa, minimo=ANEL_MIN_LISO):
    """Pinta `caixa` (x1, y1, x2, y2 no recorte, fim exclusivo) com a cor do fundo do anel em volta —
    só se o anel é liso (>= `minimo` dele é a mesma cor). Muta `img`. Devolve a cor usada ou None
    (não mexeu)."""
    from PIL import ImageDraw
    cor = _cor_do_anel(img, caixa, minimo)
    if cor is None:
        return None
    W, H = img.size
    x1, y1 = max(0, int(caixa[0])), max(0, int(caixa[1]))
    x2, y2 = min(W, int(caixa[2])), min(H, int(caixa[3]))
    ImageDraw.Draw(img).rectangle((x1, y1, x2 - 1, y2 - 1), fill=cor)
    return cor


NUCLEO_FOLGA = 3              # px em volta de cada nó de desenho que é pintado SEMPRE (o que vaza além
#                               disso — círculo/sombra do avatar — some igual: não chega na borda da caixa)
NUCLEO_CHEIO = 0.72           # contêiner com >= 72% de px "não fundo" = forma cheia (pílula ~0.85,
#                               círculo ~0.78) → núcleo; a nota (dica+avatar ~0.6) e a fileira (~0.45) não


def _pintar_preservando(img, caixa, nucleos, cor):
    """Pinta `caixa` com `cor` (o fundo), MAS preserva o que ENTRA de fora dela — alça, cadarço,
    barra da peça que cruza a borda da caixa (o contêiner da nota é mais alto que o desenho e a caixa
    pegava a ponta do cadarço, que saía cortada reto). Só não preserva dentro dos `nucleos` (os
    nós-folha do overlay + NUCLEO_FOLGA): ali é overlay, pinta sempre. Borda da caixa que é a borda
    da FOTO não conta como "de fora" (o avatar cortado no pé da mídia não pode virar peça). Muta
    `img`. True se pintou."""
    import numpy as np
    from PIL import Image
    W, H = img.size
    x1, y1 = max(0, int(caixa[0])), max(0, int(caixa[1]))
    x2, y2 = min(W, int(caixa[2])), min(H, int(caixa[3]))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return False
    a = np.asarray(img.crop((x1, y1, x2, y2)).convert("RGB"), dtype=np.int16)
    h, w = a.shape[:2]
    marca = np.abs(a - np.array(cor, np.int16)).max(axis=2) > CANTO_TOL   # não é fundo
    nuc = np.zeros((h, w), bool)
    f = NUCLEO_FOLGA
    for n in nucleos:
        n1, m1, n2, m2 = n[:4]
        if len(n) > 4 and not n[4]:               # contêiner: só se é forma cheia na foto
            dentro = marca[max(0, m1 - y1):max(0, m2 - y1), max(0, n1 - x1):max(0, n2 - x1)]
            if dentro.size == 0 or dentro.mean() < NUCLEO_CHEIO:
                continue
        nuc[max(0, m1 - f - y1):max(0, m2 + f - y1), max(0, n1 - f - x1):max(0, n2 + f - x1)] = True
    livre = marca & ~nuc
    semente = np.zeros((h, w), bool)
    if y1 > 0:
        semente[0, :] = True
    if y2 < H:
        semente[-1, :] = True
    if x1 > 0:
        semente[:, 0] = True
    if x2 < W:
        semente[:, -1] = True
    chega = livre & semente
    if chega.any():
        for _ in range(h + w):                    # inunda a partir da borda, sem atravessar o núcleo
            novo = chega.copy()
            novo[1:, :] |= chega[:-1, :]
            novo[:-1, :] |= chega[1:, :]
            novo[:, 1:] |= chega[:, :-1]
            novo[:, :-1] |= chega[:, 1:]
            novo &= livre
            if np.array_equal(novo, chega):
                break
            chega = novo
    manter = chega.copy()                         # + 1px de contorno (a borda antisserrilhada da alça)
    manter[1:, :] |= chega[:-1, :]
    manter[:-1, :] |= chega[1:, :]
    manter[:, 1:] |= chega[:, :-1]
    manter[:, :-1] |= chega[:, 1:]
    manter &= ~nuc
    out = a.copy()
    out[~manter] = cor
    img.paste(Image.fromarray(out.astype(np.uint8), "RGB"), (x1, y1))
    return True


def _soma_janela(x, r):
    """Soma de cada px com os vizinhos numa janela (2r+1)² (fora da imagem = 0). `x` = array 2D ou
    3D (h, w[, canais]) do numpy."""
    import numpy as np
    k = 2 * r + 1
    pad = ((r + 1, r), (r + 1, r)) + ((0, 0),) * (x.ndim - 2)
    c = np.pad(x, pad).cumsum(0).cumsum(1)
    return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]


def _preencher_suave(img, caixa):
    """Preenche `caixa` (x1, y1, x2, y2 no recorte, fim exclusivo) "puxando" a cor das beiradas pra
    dentro, camada por camada (inpaint simples), e alisa o miolo. Pro overlay em cima de fundo que
    não é liso: sombra, degradê, a própria peça — fica uma mancha suave, mas o balão some. Muta
    `img`. True se pintou."""
    import numpy as np
    from PIL import Image
    W, H = img.size
    x1, y1 = max(0, int(caixa[0])), max(0, int(caixa[1]))
    x2, y2 = min(W, int(caixa[2])), min(H, int(caixa[3]))
    if x2 - x1 < 2 or y2 - y1 < 2:
        return False
    m = 12                                        # moldura de referência em volta
    rx1, ry1, rx2, ry2 = max(0, x1 - m), max(0, y1 - m), min(W, x2 + m), min(H, y2 + m)
    a = np.asarray(img.crop((rx1, ry1, rx2, ry2)).convert("RGB"), dtype=np.float32)
    conhecido = np.ones(a.shape[:2], np.float32)
    conhecido[y1 - ry1:y2 - ry1, x1 - rx1:x2 - rx1] = 0
    buraco = conhecido == 0
    if conhecido.sum() < 40:
        return False                              # caixa cobre o recorte quase todo: não inventa
    out = a * conhecido[..., None]
    for _ in range(max(a.shape[:2])):
        if conhecido.all():
            break
        num = _soma_janela(out, 2)                # `out` é 0 onde ainda não se sabe
        den = _soma_janela(conhecido, 2)
        frente = (conhecido == 0) & (den >= 3)    # a camada que encosta no que já se sabe
        if not frente.any():
            break
        out[frente] = num[frente] / den[frente][:, None]
        conhecido[frente] = 1
    if not conhecido.all():
        return False
    um = np.ones(a.shape[:2], np.float32)
    for r in (4, 2):                              # alisa só o miolo (tira as "estrias" da propagação)
        lisa = _soma_janela(out, r) / _soma_janela(um, r)[..., None]
        out[buraco] = lisa[buraco]
    img.paste(Image.fromarray(np.clip(out, 0, 255).astype(np.uint8), "RGB"), (rx1, ry1))
    return True


def _limpar_canto(img, reg):
    """2ª passada num canto `reg` (x1, y1, x2, y2 no recorte): se a borda da região é fundo liso e
    dentro dela há UMA mancha compacta que não encosta nessa borda (= overlay solto em cima do fundo),
    pinta a mancha (com folga) com a cor do fundo. Muta `img`. Devolve a caixa pintada ou None."""
    from PIL import Image, ImageDraw
    pedaco = img.crop(reg)
    w, h = pedaco.size
    b = CANTO_BORDA
    if w < 6 * b or h < 6 * b:
        return None
    borda = Image.new("L", (w, h), 255)
    ImageDraw.Draw(borda).rectangle((b, b, w - b - 1, h - b - 1), fill=0)
    cor = _fundo_liso(pedaco, borda)
    if cor is None:
        return None                               # o canto não é fundo liso (a peça chega ali)
    mancha = _dif_max(pedaco, cor).point(lambda v: 255 if v > CANTO_TOL else 0)
    bb = mancha.getbbox()
    if not bb:
        return None                               # canto já limpo
    bx1, by1, bx2, by2 = bb
    if bx1 < b or by1 < b or bx2 > w - b or by2 > h - b:
        return None                               # encosta na borda = continua pra fora (é a peça)
    bw, bh = bx2 - bx1, by2 - by1
    if bw < 10 or bh < 10 or bw > w * 0.8 or bh > h * 0.8:
        return None
    if not (0.4 <= bw / float(bh) <= 3.5):
        return None
    if mancha.crop(bb).histogram()[255] < bw * bh * CANTO_DENSIDADE:
        return None                               # pontinhos espalhados (textura/sujeira), não um selo
    f = max(4, int(max(bw, bh) * 0.12))
    cx1, cy1 = max(b, bx1 - f), max(b, by1 - f)
    cx2, cy2 = min(w - b, bx2 + f), min(h - b, by2 + f)
    caixa = (reg[0] + cx1, reg[1] + cy1, reg[0] + cx2, reg[1] + cy2)
    ImageDraw.Draw(img).rectangle((caixa[0], caixa[1], caixa[2] - 1, caixa[3] - 1), fill=cor)
    return caixa


def _limpar_capa(img, overlays, nucleos=None):
    """Limpa os overlays do IG no recorte `img` (RGB, mutado). `overlays` = retângulos JÁ em
    coordenadas do recorte (vindos dos nós do dump); `nucleos` = pra cada overlay, os nós-folha dele
    (mesmas coordenadas) — com eles a pintura preserva o que entra de fora da caixa. Devolve
    (img, info) — info diz o que foi pintado/pulado (vai pro debug de calibração)."""
    info = {"nos": [list(r) for r in overlays], "limpos": [], "suaves": [], "pulados": [],
            "cantos": []}
    W, H = img.size
    caixas = []
    for i, (x1, y1, x2, y2) in enumerate(overlays):
        if y1 >= H * OVERLAY_FAIXA_BAIXO and H - y2 <= OVERLAY_ENCOSTA_BAIXO:
            y2 = H                                # fileira de notas colada no pé: vai até o pé
        nucs = list(nucleos[i]) if nucleos and i < len(nucleos) and nucleos[i] else []
        caixas.append(((x1, y1, x2, y2), nucs))

    def _pintar(caixa, nucs, minimo):
        cor = _cor_do_anel(img, caixa, minimo)
        if cor is None:
            return False
        if nucs:
            try:
                return _pintar_preservando(img, caixa, nucs, cor)
            except Exception:
                pass                              # sem numpy/erro → retângulo cheio (antigo)
        return _pintar_se_liso(img, caixa, minimo) is not None

    def _liso(c, nucs):
        x1, y1, x2, y2 = c
        lado = max(x2 - x1, y2 - y1)
        for fmin, ffrac in OVERLAY_FOLGAS:
            f = max(fmin, int(lado * ffrac))
            if _pintar((x1 - f, y1 - f, x2 + f, y2 + f), nucs, ANEL_MIN_LISO):
                return True
        return False

    # cor lisa primeiro; quem falhou tenta de novo depois (o anel podia estar pegando o avatar
    # VIZINHO, que só agora foi pintado)
    falta = caixas
    for _ in range(3):
        resto = []
        for c, nucs in falta:
            if _liso(c, nucs):
                info["limpos"].append(list(c))
            else:
                resto.append((c, nucs))
        progresso = len(resto) < len(falta)
        falta = resto
        if not falta or not progresso:
            break
    # o que sobrou não tem fundo liso em volta: se ainda é MAIORIA fundo (alça/cadarço cruzando o
    # branco), pinta com o fundo; se é tecido mesmo, preenchimento suave. Nunca deixa o balão.
    f = OVERLAY_FOLGAS[-1][0]
    for (x1, y1, x2, y2), nucs in falta:
        caixa = (x1 - f, y1 - f, x2 + f, y2 + f)
        if _pintar(caixa, nucs, ANEL_MIN_MAIORIA):
            info["limpos"].append([x1, y1, x2, y2])
            continue
        try:
            ok = _preencher_suave(img, caixa)
        except Exception:
            ok = False
        (info["suaves"] if ok else info["pulados"]).append([x1, y1, x2, y2])
    if LIMPAR_CANTOS:
        for nome in CANTOS:
            for frac in CANTO_LADOS:
                lado = int(min(W, H) * frac)
                reg = {"inf_esq": (0, H - lado, lado, H), "sup_dir": (W - lado, 0, W, lado),
                       "inf_dir": (W - lado, H - lado, W, H), "sup_esq": (0, 0, lado, lado)}[nome]
                caixa = _limpar_canto(img, reg)
                if caixa:
                    info["cantos"].append([nome] + list(caixa))
                    break
    return img, info


def _jpeg(img, quality=82):
    """PIL.Image → bytes JPEG (ou None)."""
    try:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=quality)
        return buf.getvalue()
    except Exception:
        return None


def _capturar_capa(d, espera=0.0, code=None):
    """Screencap + recorte pelas bounds da mídia + limpeza dos overlays do IG. Devolve
    {"jpeg": bytes|None, "limpos": n} (n = overlays pintados). O dump da mídia e o dos overlays é o
    MESMO (tirado antes do print). `espera` = segundos pros overlays assentarem antes do dump+print.
    A limpeza é best-effort: se falhar, sai o recorte cru (o comportamento antigo)."""
    out = {"jpeg": None, "limpos": 0}
    if espera:
        _sleep(espera, espera * 1.5)
    xml = _dump(d)
    bb = _bounds_midia(d, xml)
    if not bb:
        return out
    im = _print_tela(d)
    if im is None:
        return out
    try:
        iw, ih = im.size
        W, H = d.window_size()
        # o screencap pode vir em resolução diferente do window_size → escala as bounds
        sx, sy = iw / float(W or iw), ih / float(H or ih)
        x1, y1, x2, y2 = (int(bb[0] * sx), int(bb[1] * sy), int(bb[2] * sx), int(bb[3] * sy))
        x1, y1 = max(0, x1), max(0, y1)
        x2, y2 = min(iw, x2), min(ih, y2)
        if x2 - x1 < 4 or y2 - y1 < 4:
            return out
        recorte = im.crop((x1, y1, x2, y2)).convert("RGB")
    except Exception:
        return out
    final, info = recorte, None
    try:
        def _rec(r):                              # tela → coordenadas do recorte
            return (int(r[0] * sx) - x1, int(r[1] * sy) - y1, int(r[2] * sx) - x1,
                    int(r[3] * sy) - y1)
        grupos = _overlays_na_midia(xml, bb, com_nucleos=True)
        ovs = [_rec(g) for g, _ in grupos]
        nucs = [[_rec(n) + tuple(n[4:]) for n in ns] for _, ns in grupos]
        final, info = _limpar_capa(recorte.copy(), ovs, nucs)
        info["nucleos"] = [[list(n) for n in ns] for ns in nucs]
        info["midia"] = list(bb)
        info["escala"] = [round(sx, 4), round(sy, 4)]
        out["limpos"] = len(info["limpos"]) + len(info["suaves"]) + len(info["cantos"])
    except Exception as e:
        final, info = recorte, {"erro": repr(e)}   # falhou a limpeza → recorte cru
    # tira uma beirada de ~10px: o card da mídia tem cantos arredondados e uma linha de borda — sem
    # isso a foto sai com um fio escuro nas laterais e nos 4 cantos (visto nos prints reais)
    try:
        m = max(1, int(round(10 * sx)))
        if final.width > 8 * m and final.height > 8 * m:
            final = final.crop((m, m, final.width - m, final.height - m))
    except Exception:
        pass
    out["jpeg"] = _jpeg(final) or (_jpeg(recorte) if final is not recorte else None)
    _debug_foto(im, xml, out["jpeg"], info, code)
    return out


def _recortar_capa(d, espera=0.0, code=None):
    """Screencap + recorte pelas bounds da mídia (já sem os overlays do IG) → JPEG em memória
    (bytes). None se não deu."""
    return _capturar_capa(d, espera=espera, code=code).get("jpeg")


def _ler_taken_at(d):
    """Best-effort: epoch da data do post pelo cabeçalho. O device raramente mostra data absoluta no
    feed de detalhe (costuma ser "há 2 d"), então aqui devolve None e deixa o backend sem taken_at —
    o backend tolera null. (A CONFIRMAR ao vivo: se o header trouxer data absoluta em content-desc,
    dá pra parsear aqui.)"""
    return None


def _ler_legenda_uma_vez(d, log=None):
    """Expande e lê a legenda do post em foco. Devolve (legenda_limpa, ok): ok = achou o #p, OU achou
    a legenda DE VERDADE (o nó começa com o @ da loja) inteira e sem #p (post sem código)."""
    expandir_legenda(d, log)
    _bb, bruta = _bounds_caption(d)          # relê DEPOIS de expandir
    bruta = bruta or ""
    limpa = _legenda_limpa(bruta)            # unescape + tira prefixo do @ e o "… mais" do fim
    if _RE_NUM_PECA.search(limpa):
        return limpa, True
    low = html.unescape(bruta).rstrip().lower()
    eh_legenda = str(PERFIL_LOJA).lower() in low
    return limpa, (eh_legenda and not low.endswith(MAIS_FIM))


def ler_legenda_atual(d, log=None):
    """Leitura LEVE do post em foco: só a legenda (já limpa). Sem print nem copiar-link — serve pra
    reconhecer post já conhecido pelo #p enquanto a fase A desce.
    O #p fica nas hashtags, no FIM da legenda: em post alto (foto 4:5 no tablet) a legenda fica abaixo
    da dobra ou cortada no "mais" e a 1ª leitura não pega. Nesse caso rola o post ~450px pra cima,
    relê e volta exatamente o que andou (medido pela base da mídia) — o print da capa depende disso."""
    limpa, ok = _ler_legenda_uma_vez(d, log)
    if ok or not _no_ig(d) or not _no_post(d):   # nunca rola a tela fora de um post (ex.: o perfil)
        return limpa
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    antes = _bounds_midia(d)
    mid = antes or (0, int(H * 0.25), W, int(H * 0.8))
    cx = (mid[0] + mid[2]) // 2
    y1 = (mid[1] + mid[3]) // 2
    y2 = max(int(H * 0.12), y1 - 450)
    d.swipe(cx, y1, cx, y2, 0.5)             # devagar: rola o post sem pular pro próximo
    _sleep(0.8, 1.2)
    depois = _bounds_midia(d)
    limpa2, ok2 = _ler_legenda_uma_vez(d, log)
    andou = (antes[3] - depois[3]) if (antes and depois) else (y1 - y2)
    if andou > 20:
        d.swipe(cx, y2, cx, min(H - 10, y2 + andou), 0.5)
        _sleep(0.6, 1.0)
    if ok2 or _RE_NUM_PECA.search(limpa2):
        return limpa2
    return limpa


_DEBUG_MAX = 3                               # posts problemáticos salvos (dump + print) por run
_DEBUG_FOTOS_MAX = 3                         # 1ªs fotos de cada run salvas p/ calibrar a limpeza
_debug_fotos = {"n": 0}                      # contador das fotos salvas (zerado no começo do run())
_DEBUG_DIR = None                            # None = bots/brecho_scraper/debug/ (o teste offline troca)


def _pasta_debug():
    """Pasta de debug (gitignored). Cria se não existir."""
    import os
    pasta = _DEBUG_DIR or os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug")
    os.makedirs(pasta, exist_ok=True)
    return pasta


def _debug_foto(tela, xml, jpeg, info, code=None):
    """CALADO (sem log): guarda as _DEBUG_FOTOS_MAX primeiras fotos da run pra calibrar a limpeza
    dos overlays — o print INTEIRO (PNG), o dump usado no recorte (XML), o recorte final já limpo
    (JPEG) e o que a limpeza decidiu (JSON: nós achados, pintados, pulados, cantos)."""
    import json
    import os
    if _debug_fotos["n"] >= _DEBUG_FOTOS_MAX:
        return
    _debug_fotos["n"] += 1
    try:
        nome = re.sub(r"[^A-Za-z0-9_-]", "", str(code or "")) or "sem-code"
        base = os.path.join(_pasta_debug(),
                            f"{time.strftime('%Y%m%d-%H%M%S')}-foto{_debug_fotos['n']}-{nome}")
        if tela is not None:
            tela.save(base + "-tela.png")
        with open(base + "-dump.xml", "w", encoding="utf-8") as f:
            f.write(xml or "")
        if jpeg:
            with open(base + "-limpa.jpg", "wb") as f:
                f.write(jpeg)
        with open(base + "-limpeza.json", "w", encoding="utf-8") as f:
            json.dump(info or {}, f, ensure_ascii=False)
    except Exception:
        pass


def _salvar_debug(d, motivo):
    """Guarda o dump + o print da tela do post em foco em bots/brecho_scraper/debug/ pra
    diagnóstico. Devolve o nome base do arquivo (ou '' se não deu)."""
    import os
    try:
        base = os.path.join(_pasta_debug(), f"{time.strftime('%Y%m%d-%H%M%S')}-{motivo}")
        with open(base + ".xml", "w", encoding="utf-8") as f:
            f.write(_dump(d) or "")
        im = _print_tela(d)
        if im is not None:
            im.save(base + ".png")
        return os.path.basename(base)
    except Exception:
        return ""


def completar_post(d, legenda, log=None):
    """Completa a leitura do post em foco (legenda já lida): print da capa + shortcode."""
    imagem = _recortar_capa(d)
    taken = _ler_taken_at(d)
    # o shortcode é por último: abre/fecha a folha de enviar e pode mexer na tela
    code = _copiar_shortcode(d, log)
    return {"code": code, "legenda": legenda, "imagem_bytes": imagem, "taken_at": taken}


def ler_post_atual(d, log=None):
    """Lê o post em foco. Devolve {code, legenda, imagem_bytes, taken_at}. `code` pode vir None (aí o
    chamador pula o post, sem derrubar a run)."""
    return completar_post(d, ler_legenda_atual(d, log), log)


def _assinatura_post(d):
    """Fingerprint BARATO do post em foco (sem a dança do copiar-link): início da legenda + bucket do
    topo da mídia. Serve pra proximo_post saber se o feed ANDOU de verdade."""
    _bb, legenda = _bounds_caption(d)
    mid = _bounds_midia(d)
    topo = (mid[1] // 25) if mid else -1
    return ((legenda or "")[:60], topo)


def proximo_post(d, log=None, antes=None, tentativas=MAX_SWIPES_SEM_MUDAR):
    """PRIMITIVA de swipe: desliza pra cima (rumo aos posts mais ANTIGOS) e CONFIRMA que o feed andou
    (a assinatura — início da legenda + topo da mídia — mudou; equivale a "o shortcode/legenda
    mudou", sem repetir o copiar-link a cada swipe). Devolve True se avançou, False após `tentativas`
    sem andar. `antes` = assinatura do post atual (se None, lê agora).

    OBS: num teste ao vivo (perfil aberto por URL handler) o swipe NÃO avançava — por isso o
    Navegador usa isto no modo 'swipe'/'auto' e cai pra navegação célula-a-célula quando falha."""
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    if antes is None:
        antes = _assinatura_post(d)
    for _ in range(max(1, int(tentativas))):
        x = W // 2
        d.swipe(x, int(H * 0.72), x, int(H * 0.30), 0.18)   # flick curto e firme
        _sleep(1.2, 2.0)
        if _tela_tem(d, INDISPONIVEL_TXT) or not _no_ig(d):
            return True            # tem conteúdo (ainda que indisponível) — o raspar trata/pula
        if _assinatura_post(d) != antes:
            return True
    return False


# ════════════════════════════ navegação PLUGÁVEL (swipe-feed vs célula-a-célula) ════════════════
# PENDÊNCIA pra afinar no teste real: o swipe no feed de detalhe nem sempre avança (visto ao abrir o
# perfil por URL handler). A estratégia fica isolada aqui, no Navegador:
#   - "swipe":  só desliza no feed (rápido; depende do feed de detalhe paginar no swipe);
#   - "celula": volta pro grid e abre a próxima célula por (linha, coluna) do content-desc (lento,
#               porém robusto — não depende do swipe);
#   - "auto" (padrão): tenta swipe e, quando ele NÃO avança, passa a célula-a-célula pro resto da run.
# Troque a estratégia pelo param `navegacao` do job (ou ajuste NAVEGACAO_PADRAO).

def _no_grid(d):
    """Estamos no GRID do perfil? (há célula image_button OU algum content-desc 'linha L, coluna C')."""
    if _bounds_por_id(d, GRID_CELL_IDS):
        return True
    for t, _bb in _nodes(_dump(d)):
        desc = _attr(t, "content-desc")
        if _RE_GRID_POS.search(desc) or _RE_GRID_POS_EN.search(desc):
            return True
    return False


def _voltar_ao_grid(d, log=None):
    """Sai do post (back) até o grid do perfil reaparecer. Trata o IG ter ido pra fora (Custom Tab).
    True se chegou no grid."""
    for _ in range(7):
        if not _no_ig(d):
            af.garantir_app(d, log)
        if _no_grid(d):
            return True
        d.press("back")
        _sleep(0.9, 1.5)
    return _no_grid(d)


def _celulas_visiveis(d):
    """Mapa {(linha, coluna): bounds} das células do grid visíveis agora (pelo content-desc)."""
    out = {}
    for t, bb in _nodes(_dump(d)):
        desc = _attr(t, "content-desc")
        m = _RE_GRID_POS.search(desc) or _RE_GRID_POS_EN.search(desc)
        if m:
            out[(int(m.group(1)), int(m.group(2)))] = bb
    return out


def _detectar_cols(d):
    """Nº de colunas do grid pelo maior 'coluna C' visível (tablet costuma ser 3). Default 3."""
    cels = _celulas_visiveis(d)
    cols = max((c for (_l, c) in cels), default=0)
    return cols or 3


def _celula_por_posicao(d, linha, coluna, log=None, max_scrolls=45):
    """Bounds da célula (linha, coluna) do grid (1-based, ABSOLUTAS, como no content-desc).
    Rola o grid pros DOIS lados: voltar de um post pode deixar o grid noutra altura (o IG sincroniza
    o grid com o último post visto), e aí a célula alvo fica ACIMA das visíveis. No fim do que já
    carregou o IG busca mais miniaturas aos poucos (o grid fica parado uns segundos): só conclui que
    acabaram os posts depois de várias tentativas paradas, com espera. None = não há essa célula."""
    try:
        W, H = d.window_size()
    except Exception:
        W, H = 900, 1600
    prev = None
    parados = 0
    for _ in range(max_scrolls):
        cels = _celulas_visiveis(d)
        if (linha, coluna) in cels:
            return cels[(linha, coluna)]
        linhas_vis = [l for (l, _c) in cels]
        if linhas_vis and linha < min(linhas_vis):
            # alvo ACIMA do que está na tela → rola o grid pra cima
            d.swipe(W // 2, int(H * 0.35), W // 2, int(H * 0.70), random.uniform(0.35, 0.5))
            _sleep(0.9, 1.5)
            prev, parados = None, 0
            continue
        chave = tuple(sorted(cels))
        if chave and chave == prev:
            parados += 1
            if parados >= 4:
                return None                   # parado de verdade (fundo do grid): acabaram os posts
            _sleep(1.5, 2.5)                  # IG carregando mais miniaturas — espera e tenta de novo
        else:
            parados = 0
        prev = chave
        d.swipe(W // 2, int(H * 0.72), W // 2, int(H * 0.32), random.uniform(0.35, 0.5))   # lento: sem inércia
        _sleep(0.9, 1.5)
    return None


def _celula_parada(d, linha, coluna, bb, tentativas=4):
    """Espera o grid PARAR de rolar (o swipe tem inércia): relê a célula até as bounds repetirem.
    Tocar com o grid ainda andando só para a rolagem — não abre o post."""
    for _ in range(tentativas):
        _sleep(0.5, 0.8)
        nova = _celulas_visiveis(d).get((linha, coluna))
        if not nova or nova == bb:
            return nova or bb
        bb = nova
    return bb


def _abrir_celula(d, idx, cols, log=None, tentativas=2):
    """Volta pro grid e abre a célula de índice `idx` (0-based, na ordem do grid: novo→antigo).
    idx → (linha, coluna) 1-based com `cols` colunas. Confere que entrou MESMO num post (_no_post).
    Devolve True = post aberto; False = a célula existe mas não abriu como post (reel/outro viewer, ou
    o toque não pegou de novo) → o chamador pula ela; None = não há essa célula / perdi o grid (fim)."""
    linha, coluna = idx // cols + 1, idx % cols + 1
    for tent in range(tentativas):
        if not _voltar_ao_grid(d, log):
            _log(log, "  (não consegui voltar ao grid pra navegar célula-a-célula)")
            return None
        bb = _celula_por_posicao(d, linha, coluna, log)
        if not bb:
            return None
        bb = _celula_parada(d, linha, coluna, bb)
        d.click((bb[0] + bb[2]) // 2, (bb[1] + bb[3]) // 2)
        for _ in range(8):
            _sleep(0.8, 1.3)
            if _no_post(d):
                return True
        if not _no_grid(d):
            _log(log, f"  (miniatura {linha}/{coluna} não abre como post — reel? — pulei)")
            return False
    return False


class Navegador:
    """Avança de um post pro próximo com a estratégia escolhida (ver bloco acima). `idx` é o índice
    0-based do post aberto AGORA (0 = 1ª célula, já aberta por entrar_no_feed)."""

    def __init__(self, d, modo=NAVEGACAO_PADRAO, log=None):
        self.d = d
        self.modo = modo if modo in ("auto", "swipe", "celula") else "auto"
        self.log = log
        self.idx = 0
        self.cols = None

    def _colunas(self):
        if self.cols is None:
            # precisa estar no grid pra medir; se não estiver, assume 3 (tablet) sem navegar
            self.cols = _detectar_cols(self.d) if _no_grid(self.d) else 3
        return self.cols

    def avancar(self):
        """Posiciona no PRÓXIMO post. True se abriu um novo; False se acabaram os posts."""
        if self.modo in ("auto", "swipe"):
            antes = _assinatura_post(self.d)
            if proximo_post(self.d, self.log, antes=antes):
                self.idx += 1
                return True
            if self.modo == "swipe":
                _log(self.log, "feed não andou mais (modo swipe) — fim dos posts")
                return False
            _log(self.log, "swipe não avançou — passando pra navegação célula-a-célula (fallback)")
            self.modo = "celula"
            self.cols = None            # mede as colunas agora que vamos ao grid
        falhas = 0
        while True:
            r = _abrir_celula(self.d, self.idx + 1, self._colunas(), self.log)
            self.idx += 1
            if r:
                return True
            if r is None:
                _log(self.log, "chegou no fim do feed")
                return False
            falhas += 1                 # célula que não abre como post: pula pra seguinte
            if falhas >= 5:
                _log(self.log, "5 células seguidas sem abrir post — parando a navegação")
                return False

    def garantir_post(self):
        """Confere que o post da vez está MESMO aberto (um back ou toque perdido pode ter deixado o bot
        no perfil). Se não estiver, reabre a célula atual pelo grid. True se está num post."""
        if _no_post(self.d):
            return True
        return bool(_abrir_celula(self.d, self.idx, self._colunas(), self.log))


def conteudo_indisponivel(d, log=None):
    """IG jogou pro Custom Tab/chromium (app_current != IG) OU mostrou 'conteúdo indisponível'.
    Volta pro IG (back) e devolve True (o chamador PULA o post, sem derrubar a run)."""
    fora = not _no_ig(d)
    aviso = _tela_tem(d, INDISPONIVEL_TXT)
    if not (fora or aviso):
        return False
    _log(log, "  (post indisponível — pulei)")
    for _ in range(4):
        d.press("back")
        _sleep(0.8, 1.4)
        if _no_ig(d) and not _tela_tem(d, INDISPONIVEL_TXT):
            break
    return True


# ════════════════════════════ raspagem (loop) ════════════════════════════
def _num_peca(legenda):
    """#p<num> da legenda (o mesmo código que o backend usa pra ordenar/casar) ou None."""
    m = _RE_NUM_PECA.search(legenda or "")
    return int(m.group(1)) if m else None


def _desc_peca(legenda, num=None):
    """Rótulo curtinho pro [progress] AO VIVO (o espelho do app imprime uma linha por peça + a
    barra/LA mostram a peça da vez): '#p<n> <nome curto>' + se a legenda diz vendida. Heurística
    barata, SEM o parser do backend: nome = 1ª linha de conteúdo da legenda já limpa; vendida =
    palavra 'vendid*'. Devolve (ident, vendida_bool)."""
    leg = legenda or ""
    n = num if num is not None else _num_peca(leg)
    nome = ""
    for ln in leg.splitlines():
        s = ln.strip().lstrip("|").strip()
        if not s or re.search(r"vendid[oa]", s, re.I):   # pula vazio e o marcador ❌VENDIDO❌
            continue
        nome = re.sub(r"\s+", " ", s)[:28]
        break
    ident = ((f"#p{n} " if n else "") + nome).strip() or (f"#p{n}" if n else "peça")
    return ident, bool(re.search(r"vendid[oa]", leg, re.I))


# pistas de que a legenda é PEÇA (não divulgação) — espelha o eh_peca do parser do backend, mas só
# pro RÓTULO ao vivo (o backend é quem decide de verdade e ignora a divulgação no parse_post).
_RE_PECA_PISTA = re.compile(r"#p\d|r\$|\btam\b|tam\.|condi|vendid|\d\s*cm", re.I)


def _parece_peca(legenda):
    return bool(_RE_PECA_PISTA.search(legenda or ""))


def raspar(d, *, known_codes=frozenset(), known_nums=frozenset(), sem_link=(), conhecidos=frozenset(),
           profundidade=None, full=False, limite=LIMITE_PADRAO, navegacao=NAVEGACAO_PADRAO, log=None,
           parar=None, total_base=0):
    """FASE A — lê o feed do TOPO (mais novo → mais antigo) coletando os posts NOVOS (crus, sem
    parsear). O avanço é PLUGÁVEL via `navegacao` (ver Navegador): 'celula' (padrão: tap célula → lê
    → back → próxima célula), 'swipe' ou 'auto'. PARA quando:
      - o code (ou o #p da legenda) do post já está em `known_codes`/`known_nums` (dali pra trás é
        tudo conhecido), NÃO é `full` e não falta nenhuma peça `sem_link`;
      - atinge `limite` (cap de segurança) / `parar()` / a navegação não avança mais.

    `sem_link` = #p de peças POSTADAS (drop publicado) que o app ainda não tem o link — a fase B não
    consegue abrir. Enquanto faltar alguma, a fase A NÃO para no 1º conhecido: desce passando batido
    (só lê a legenda, sem print nem copiar-link) pelos posts já conhecidos (`conhecidos`/`known_nums`)
    até achar cada uma, confere (VENDIDA/DISPONÍVEL) e manda. Para quando achou todas, ou ao passar
    de `profundidade` posts (= até o drop mais antigo com peça sem link à venda; drop mais antigo
    que isso não é relido).
    Dedupe por shortcode/#p. Ritmo humano entre posts. Devolve [{code, legenda, imagem_bytes,
    taken_at}]. `total_base` só alimenta o denominador do [progress]."""
    parar = parar or (lambda: False)
    nav = Navegador(d, navegacao, log)
    vistos = set()
    vistos_num = set()
    posts = []
    pendentes = {int(n) for n in (sem_link or ()) if n is not None} if not full else set()
    passou_conhecido = False            # já cruzou o 1º post conhecido (fim da zona de posts novos)
    visitados = 0
    pulados = 0
    debug_salvos = 0
    teto = int(profundidade) if profundidade else limite + 200
    while True:
        if parar():
            _log(log, "parada solicitada — encerrando a fase A")
            break
        if len(posts) >= limite:
            _log(log, f"cap de segurança: {limite} posts — parando a fase A")
            break
        if pendentes and passou_conhecido and visitados >= teto:
            _log(log, "Não achou ··· " + ", ".join(f"#p{n}" for n in sorted(pendentes, reverse=True))
                      + " (desceu até o drop dela)")
            break
        if conteudo_indisponivel(d, log):
            if not nav.avancar():
                break
            continue
        # só lê se está MESMO dentro de um post (senão lia a página do perfil como legenda)
        if not nav.garantir_post():
            _log(log, f"  (não consegui abrir o post da célula {nav.idx + 1} — pulo)")
            if not nav.avancar():
                break
            continue
        visitados += 1

        legenda = ler_legenda_atual(d, log)     # leitura LEVE (só a legenda)
        num = _num_peca(legenda)
        alvo = num is not None and num in pendentes

        # descendo atrás de peça sem link: post já conhecido pelo #p — ou SEM #p na legenda
        # (divulgação/reels; não tem como ser a peça procurada, e aqui embaixo nada é novo) — passa
        # batido, sem print nem copiar-link. A cada 5 pulados solta uma linha pra tela não ficar muda.
        if (pendentes and passou_conhecido and not alvo
                and (num is None or num in conhecidos or num in known_nums)):
            pulados += 1
            if num is None and debug_salvos < _DEBUG_MAX:
                debug_salvos += 1
                arq = _salvar_debug(d, "sem-p")
            if pulados % 5 == 1:
                _log(log, f"[progress] {len(posts)} {max(1, total_base + len(posts))} DESCENDO "
                          + (f"#p{num}" if num is not None else "post sem #p")
                          + " (atrás de " + ", ".join(f"#p{n}" for n in sorted(pendentes, reverse=True))
                          + f" · {visitados}/{teto})")
            if not nav.avancar():
                break
            continue

        post = completar_post(d, legenda, log)  # print da capa + link
        code = post.get("code")

        # chegou num post que o app JÁ CONHECE (code em recentes/disponiveis, ou #p conhecido)
        conhecido = (code and code in known_codes) or (num is not None and num in known_nums)
        if conhecido and not full and not alvo:
            if not pendentes:
                _log(log, "chegou nos posts que o app já conhece")
                break
            if not passou_conhecido:
                passou_conhecido = True
                _log(log, "─── Procurando peça sem link: "
                          + ", ".join(f"#p{n}" for n in sorted(pendentes, reverse=True)) + " ───")
            if not nav.avancar():
                break
            continue

        if alvo:
            # peça sem link achada: confere e manda (com o link, se o copiar funcionou agora — aí ela
            # ganha o code e da próxima vez já vai pela fase B)
            pendentes.discard(num)
            vistos_num.add(num)
            if code:
                vistos.add(code)
            posts.append(post)
            ident, vend = _desc_peca(legenda, num)
            _log(log, f"[progress] {len(posts)} {max(1, total_base + len(posts))} "
                      f"{'VENDIDA' if vend else 'DISPONÍVEL'} {ident}" + ("" if code else " (sem link)"))
            if not pendentes and passou_conhecido:
                _log(log, "achou a(s) peça(s) sem link")
                break
            _sleep(1.2, 2.6)
            if not nav.avancar():
                break
            continue

        if not code:
            # sem shortcode (SHARE não abriu). Se a legenda tem #p, ainda vale enviar: o backend
            # casa por número e aplica venda/mudanças (sem inserir peça nova). Senão não dá pra
            # casar — pula. Dedupe por #p pra não mandar o mesmo post duas vezes.
            if num is not None and num not in vistos_num:
                vistos_num.add(num)
                posts.append(post)
                ident, _vend = _desc_peca(legenda, num)
                _log(log, f"[progress] {len(posts)} {max(1, total_base + len(posts))} "
                          f"NOVO {ident} (sem link)")
                _sleep(1.2, 2.6)
            else:
                _log(log, "  (post sem #p e sem link — ignorado)")
            if not nav.avancar():
                break
            continue
        if code in vistos:
            # já lido (o feed pode repetir ao assentar) — tenta avançar; se não anda, acabou
            if not nav.avancar():
                break
            continue
        vistos.add(code)

        posts.append(post)
        if not _parece_peca(legenda):    # divulgação: o backend ignora no parse — rótulo claro aqui
            etq = "DIVULGAÇÃO (ignorado)"
        else:
            ident, vend = _desc_peca(legenda, num)
            etq = f"NOVO {ident}" + (" (já vendida)" if vend else "")
        _log(log, f"[progress] {len(posts)} {max(1, total_base + len(posts))} {etq}")

        _sleep(1.2, 2.6)                 # respiro humano entre posts
        if not nav.avancar():
            break

    if pendentes:
        _log(log, "Não achou ··· " + ", ".join(f"#p{n}" for n in sorted(pendentes, reverse=True))
                  + f" (olhou {visitados} posts)")
    _log(log, f"Posts novos ··· {len(posts)}")
    return posts


# ════════════════════════════ FASE B — conferir vendidas pelo link ════════════════════════════
def _abrir_post_por_code(d, code, log=None):
    """Abre /p/<code>/ por intent VIEW (igual story_mumu.abrir_repost), forçando o pacote do IG.
    True se o post abriu DENTRO do IG com a mídia na tela. Se cair no chromium, tenta o intent mais
    uma vez; 'indisponível' → False (o chamador pula)."""
    af.garantir_app(d, log)
    url = f"https://www.instagram.com/p/{code}/"
    for _tent in range(2):
        try:
            d.shell(f"am start -a android.intent.action.VIEW -d {url} -p {af.IG_PKG}")
        except Exception:
            return False
        _sleep(2.5, 4.0)
        no_chromium = True
        for _ in range(10):
            af.fechar_permissao_android(d, log)
            if not _no_ig(d):
                no_chromium = False
                break                      # abriu no Custom Tab/chromium — sai p/ repetir o intent
            if _tela_tem(d, INDISPONIVEL_TXT):
                return False
            if _bounds_midia(d):
                return True
            _sleep(1.0, 1.6)
        if not no_chromium:
            d.press("back")
            _sleep(1.0, 1.6)
            af.garantir_app(d, log)
    return False


_RE_PRECO = re.compile(r"r\$\s*:?\s*([\d.,]+)", re.I)   # mesmo padrão do parser do backend


def _preco_legenda(legenda):
    """R$ da legenda ("R$:27", "R$ 27", "R$27,90") → float, ou None. Só pro RÓTULO ao vivo; quem grava
    o preço de verdade é o upsert do backend."""
    m = _RE_PRECO.search(legenda or "")
    if not m:
        return None
    s = m.group(1).strip(".,")
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def _reais(v):
    v = float(v)
    return f"R$ {int(v)}" if v == int(v) else "R$ " + f"{v:.2f}".replace(".", ",")


def conferir_vendidas(d, disponiveis, *, log=None, parar=None, feitos0=0, total=0, ao_lote=None, lote=10):
    """FASE B — pra cada peça disponível {code, num, venda}, abre o post pelo link, lê a legenda
    (expandindo o 'mais') e tira o print da capa. NÃO usa o shortcode (o code já é conhecido). O backend
    detecta 'vendido'/preço na legenda e casa por code; o print migra a foto pra local permanente.
    Ritmo humano (~8-10s). `ao_lote(posts)`: chamado a cada `lote` peças lidas (e no fim com o resto) —
    o app vai se atualizando DURANTE a run e uma queda no meio não perde o que já foi conferido.
    Devolve [{code, legenda, imagem_bytes, taken_at:None}] das que deu pra ler."""
    parar = parar or (lambda: False)
    out = []
    pendente = []
    n = len(disponiveis)
    for i, alvo in enumerate(disponiveis):
        if parar():
            _log(log, "parada solicitada — encerrando a fase B")
            break
        code = (alvo or {}).get("code")
        num = (alvo or {}).get("num")
        if not code:
            continue
        passo = feitos0 + i + 1
        etq = f" #p{num}" if num else ""
        if not _abrir_post_por_code(d, code, log):
            _log(log, f"[progress] {passo} {max(1, total)} NÃO ABRIU{etq}")
            continue
        if conteudo_indisponivel(d, log):
            _log(log, f"[progress] {passo} {max(1, total)} INDISPONÍVEL{etq}")
            continue
        expandir_legenda(d, log)
        _bb, legenda = _bounds_caption(d)        # relê DEPOIS de expandir
        legenda = _legenda_limpa(legenda)        # mesma normalização da fase A (unescape + prefixo)
        imagem = _recortar_capa(d, code=code)
        post = {"code": code, "legenda": legenda, "imagem_bytes": imagem, "taken_at": None}
        out.append(post)
        pendente.append(post)
        ident, vend = _desc_peca(legenda, num)
        # STATUS em MAIÚSCULO no começo (o app colore por ele): "VENDIDA #p450 colete" / "DISPONÍVEL …"
        # Preço do post diferente do app → "ATUALIZADA … · preço R$ 28 → R$ 27" já AO VIVO.
        status, extra = ("VENDIDA" if vend else "DISPONÍVEL"), ""
        if not vend:
            try:
                p_post, p_app = _preco_legenda(legenda), float((alvo or {}).get("venda") or 0)
            except (TypeError, ValueError):
                p_post, p_app = None, 0
            if p_post and p_app and abs(p_post - p_app) > 0.009:
                status, extra = "ATUALIZADA", f" · preço {_reais(p_app)} → {_reais(p_post)}"
        _log(log, f"[progress] {passo} {max(1, total)} {status} {ident}{extra}")
        if ao_lote and len(pendente) >= lote:
            ao_lote(pendente)
            pendente = []
        if i < n - 1 and not parar():
            _sleep(8.0, 10.0)            # ritmo humano entre peças
    if ao_lote and pendente:
        ao_lote(pendente)
    _log(log, f"Conferidas ··· {len(out)} de {n}")
    return out


# ════════════════════════════ backend (urllib puro — sem dependências novas) ════════════════════
def _post_json(url, token, obj, timeout=60):
    """POST JSON com Authorization Bearer. Devolve o dict de resposta (ou {})."""
    import json
    import urllib.request
    body = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def _get_json(url, token, timeout=60):
    """GET JSON com Authorization Bearer. Devolve o dict de resposta (ou {})."""
    import json
    import urllib.request
    req = urllib.request.Request(url, method="GET")
    req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode("utf-8", "replace")
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


def buscar_alvos(brecho_base, brecho_token, log=None):
    """GET {brecho_base}/scraper/emulador/alvos no INÍCIO da run. Devolve
    {"disponiveis": [{code, num}], "recentes": [codes], "sem_link": [num], "conhecidos": [num],
    "profundidade_sem_link": int|None}. Em falha, devolve vazio (a run segue: fase A roda sem
    boundary de 'conhecido' e fase B não confere nada)."""
    url = f"{str(brecho_base).rstrip('/')}/scraper/emulador/alvos"
    vazio = {"disponiveis": [], "recentes": [], "sem_link": [], "conhecidos": [],
             "profundidade_sem_link": None}
    try:
        data = _get_json(url, brecho_token) or {}
    except Exception as e:
        _log(log, f"não consegui buscar os alvos ({e}) — sigo sem boundary/conferência")
        return vazio
    disp = data.get("disponiveis") or []
    rec = data.get("recentes") or []
    sl = data.get("sem_link") or []
    _log(log, f"À venda pra conferir ··· {len(disp)}")
    if sl:
        _log(log, "Sem link pra procurar ··· " + ", ".join(f"#p{n}" for n in sorted(sl, reverse=True)))
    return {"disponiveis": disp, "recentes": rec, "sem_link": sl,
            "conhecidos": data.get("conhecidos") or [],
            "profundidade_sem_link": data.get("profundidade_sem_link")}


def _para_wire(post):
    """Converte um post cru pro formato do contrato (imagem → base64)."""
    b64 = None
    img = post.get("imagem_bytes")
    if img:
        try:
            b64 = base64.b64encode(img).decode("ascii")
        except Exception:
            b64 = None
    return {"code": post.get("code"), "taken_at": post.get("taken_at"),
            "legenda": post.get("legenda") or "", "imagem_b64": b64}


def _merge_stats(acc, st):
    """Soma os campos numéricos das stats de cada lote (novas/atualizadas/recem_vendidas/imagens…);
    campos não-numéricos ficam com o 1º valor visto. Muta e devolve `acc`."""
    for k, v in (st or {}).items():
        if isinstance(v, bool):
            acc.setdefault(k, v)
        elif isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + v
        else:
            acc.setdefault(k, v)
    return acc


def enviar_posts(posts, *, brecho_base, brecho_token, run_id, full, done=True, log=None):
    """POST dos posts pro backend (POST {brecho_base}/scraper/emulador/posts). Loteia em blocos de
    LOTE_ENVIO (done=false nos parciais, done=true só no último). Devolve (enviados, stats), com
    `stats` ACUMULADO (somado) entre os lotes — novas/atualizadas/recem_vendidas/imagens etc."""
    url = f"{str(brecho_base).rstrip('/')}/scraper/emulador/posts"
    # manda quem tem code OU #p na legenda (post sem link mas com #p ainda casa por número)
    wire = [_para_wire(p) for p in posts if p.get("code") or _num_peca(p.get("legenda"))]
    stats = {}
    eventos = []   # log peça-a-peça que o backend devolve (NOVA/VENDIDA/ATUALIZADA…), somado dos lotes
    if not wire:
        # mesmo vazio, avisa o backend que terminou (done=true) pra fechar a run
        try:
            resp = _post_json(url, brecho_token,
                              {"run_id": run_id, "full": bool(full), "done": bool(done), "posts": []})
            _merge_stats(stats, resp)
            eventos.extend(resp.get("eventos") or [])
        except Exception as e:
            _log(log, f"falha ao avisar o backend (lote vazio): {e}")
        stats["eventos"] = eventos
        return 0, stats

    lotes = [wire[i:i + LOTE_ENVIO] for i in range(0, len(wire), LOTE_ENVIO)]
    enviados = 0
    for i, lote in enumerate(lotes):
        ultimo = (i == len(lotes) - 1)
        obj = {"run_id": run_id, "full": bool(full),
               "done": bool(done) if ultimo else False, "posts": lote}
        try:
            resp = _post_json(url, brecho_token, obj)
            _merge_stats(stats, resp)
            eventos.extend(resp.get("eventos") or [])
            enviados += len(lote)
        except Exception as e:
            _log(log, f"falha ao enviar lote {i + 1}/{len(lotes)}: {e}")
    stats["eventos"] = eventos   # sobrescreve o que o _merge_stats guardou (lista não soma)
    return enviados, stats


# ════════════════════════════ MODO FOTOS — recapturar a foto de TODAS as peças ════════════════════
# Job params["modo"] == "fotos" (disparado por POST /scraper/emulador/fotos/raspar no brechó): passa
# pelo brechó INTEIRO abrindo cada peça pelo link e tirando a foto de novo, já limpa dos overlays.
# Só a imagem volta pro backend (POST /scraper/emulador/fotos) — nenhum outro dado da peça é tocado.
#   GET  {brecho_base}/scraper/emulador/fotos/alvos → [{code, num, nome}] (com link, sem as travadas)
#   POST {brecho_base}/scraper/emulador/fotos       → {"run_id", "posts": [{code, imagem_b64}]}
#        devolve {recebidas, imagens, travadas, sem_peca, invalidas}
LOTE_FOTOS = 10                              # fotos por POST (só imagem, ~100 KB cada)
FOTOS_ESPERA = 1.0                           # s pros overlays (contador/facepile) assentarem antes do print
FOTOS_PAUSA = (8.0, 10.0)                    # respiro humano entre peças (o mesmo da fase B)
FOTOS_RESPIRO_CADA = 40                      # a cada N peças, uma pausa mais longa
FOTOS_RESPIRO = (30.0, 60.0)
FOTOS_FALHAS_SEGUIDAS = 8                    # N posts seguidos que não abrem → problema na conta auxiliar
_fotos_feitas = {}                           # {run_id: {codes já enviados}} — retoma se o agente trocar de conta


def _dormir(a, b, parar):
    """Pausa humana que ACORDA na hora se pedirem pra parar (as pausas longas travariam o stop)."""
    fim = time.time() + random.uniform(a, b)
    while time.time() < fim:
        if parar():
            return
        time.sleep(min(1.0, max(0.0, fim - time.time())))


def _ident_foto(alvo):
    """Rótulo da peça no [progress]: '#p<num> <nome curto>'."""
    num = alvo.get("num")
    nome = re.sub(r"\s+", " ", str(alvo.get("nome") or "")).strip()[:28]
    return ((f"#p{num} " if num else "") + nome).strip() or "peça"


def buscar_alvos_fotos(brecho_base, brecho_token, log=None):
    """GET {brecho_base}/scraper/emulador/fotos/alvos → [{code, num, nome}]. Falha aqui NÃO é
    ScraperAbort: é o backend do brechó (rede/rota), trocar de conta auxiliar não resolve."""
    url = f"{str(brecho_base).rstrip('/')}/scraper/emulador/fotos/alvos"
    try:
        data = _get_json(url, brecho_token)
    except Exception as e:
        raise RuntimeError(f"não consegui buscar as peças pra recapturar a foto ({e})")
    if isinstance(data, dict):
        data = data.get("alvos") or []
    return [a for a in (data or []) if isinstance(a, dict) and a.get("code")]


def enviar_fotos(fotos, *, brecho_base, brecho_token, run_id):
    """POST {brecho_base}/scraper/emulador/fotos com SÓ as imagens. Devolve as stats do backend.
    Exceção sobe (quem chama guarda o lote pra tentar de novo)."""
    url = f"{str(brecho_base).rstrip('/')}/scraper/emulador/fotos"
    return _post_json(url, brecho_token, {"run_id": run_id, "posts": list(fotos)}) or {}


def recapturar_fotos(d, params, log=None, parar=None):
    """MODO FOTOS: abre cada peça pelo link (como a fase B), tira a foto limpa e manda em lotes de
    LOTE_FOTOS. Ritmo humano (FOTOS_PAUSA + um respiro a cada FOTOS_RESPIRO_CADA). Retomável: se o
    agente trocar de conta no meio (mesmo run_id), pula as que já foram enviadas. Uma linha
    `[progress] feitos total STATUS #p<num> nome` por peça (STATUS: FOTO / NÃO ABRIU / INDISPONÍVEL /
    SEM FOTO). Devolve o mesmo formato do run()."""
    parar = parar or (lambda: False)
    base, token = params.get("brecho_base"), params.get("brecho_token")
    run_id = params.get("run_id")
    alvos = buscar_alvos_fotos(base, token, log)
    total = len(alvos)
    if str(run_id) not in _fotos_feitas:           # run nova: esquece as anteriores
        _fotos_feitas.clear()
    feitas = _fotos_feitas.setdefault(str(run_id), set())
    ja = sum(1 for a in alvos if a["code"] in feitas)
    _log(log, f"Peças com link ··· {total}")
    if ja:
        _log(log, f"Já recapturadas nesta run ··· {ja} (pulo)")
    _log(log, f"─── Recapturando fotos ({total - ja} peças) ───")

    fila = []                                      # fotos capturadas ainda não enviadas
    stats = {}
    cont = {"FOTO": 0, "NÃO ABRIU": 0, "INDISPONÍVEL": 0, "SEM FOTO": 0}
    enviadas = 0
    limpas = 0
    seguidas = 0                                   # NÃO ABRIU em sequência
    processadas = 0
    parado = False

    def _despachar(forcar=False):
        nonlocal enviadas
        if not fila or (len(fila) < LOTE_FOTOS and not forcar):
            return
        lote = list(fila)
        try:
            st = enviar_fotos(lote, brecho_base=base, brecho_token=token, run_id=run_id)
        except Exception as e:
            _log(log, f"  (falha ao enviar {len(lote)} fotos: {e} — tento de novo no próximo lote)")
            return
        del fila[:len(lote)]
        enviadas += len(lote)
        _merge_stats(stats, st)
        feitas.update(f["code"] for f in lote)

    try:
        for i, alvo in enumerate(alvos):
            if parar():
                parado = True
                _log(log, "parada solicitada — encerrando a recaptura")
                break
            code = alvo["code"]
            if code in feitas:
                continue
            if processadas and processadas % FOTOS_RESPIRO_CADA == 0:
                _log(log, f"  (respiro de alguns segundos — {processadas} peças seguidas)")
                _dormir(*FOTOS_RESPIRO, parar)
                if parar():
                    parado = True
                    _log(log, "parada solicitada — encerrando a recaptura")
                    break
            processadas += 1
            passo = i + 1
            ident = _ident_foto(alvo)
            status, extra, cap = None, "", None
            if not _abrir_post_por_code(d, code, log):
                # post apagado/indisponível também cai aqui → separa (não conta como falha da conta)
                if _tela_tem(d, INDISPONIVEL_TXT) or not _no_ig(d):
                    conteudo_indisponivel(d, None)
                    status = "INDISPONÍVEL"
                else:
                    status = "NÃO ABRIU"
            elif conteudo_indisponivel(d, None):
                status = "INDISPONÍVEL"
            else:
                cap = _capturar_capa(d, espera=FOTOS_ESPERA, code=code)
                status = "FOTO" if cap.get("jpeg") else "SEM FOTO"
            cont[status] += 1
            if status == "FOTO":
                seguidas = 0
                fila.append({"code": code,
                             "imagem_b64": base64.b64encode(cap["jpeg"]).decode("ascii")})
                if cap.get("limpos"):
                    limpas += 1
                    extra = f" (limpou {cap['limpos']})"
            elif status == "NÃO ABRIU":
                seguidas += 1
            # STATUS em MAIÚSCULO no começo (o app colore por ele)
            _log(log, f"[progress] {passo} {max(1, total)} {status} {ident}{extra}")
            _despachar()
            if seguidas >= FOTOS_FALHAS_SEGUIDAS:
                # ScraperAbort = falha "de conta": o agente tenta a próxima auxiliar (e ela retoma daqui)
                raise ScraperAbort(f"{seguidas} posts seguidos não abriram — a conta auxiliar pode "
                                   f"estar com problema")
            if i < total - 1 and not parar():
                _dormir(*FOTOS_PAUSA, parar)
    finally:
        _despachar(forcar=True)                    # manda o que sobrou (também no stop/erro)
        if fila:
            _despachar(forcar=True)                # 2ª chance (rede)
        if fila:
            _log(log, f"Fotos não enviadas ··· {len(fila)} (falha de rede)")

    imagens = int(stats.get("imagens") or 0)
    _log(log, f"Fotos capturadas ··· {cont['FOTO']} de {total - ja}")
    _log(log, f"Fotos salvas no app ··· {imagens}")
    if limpas:
        _log(log, f"Fotos com overlay limpo ··· {limpas}")
    for rot, k in (("Não abriram", "NÃO ABRIU"), ("Indisponíveis", "INDISPONÍVEL"),
                   ("Sem foto", "SEM FOTO")):
        if cont[k]:
            _log(log, f"{rot} ··· {cont[k]}")
    if stats.get("travadas"):
        _log(log, f"Travadas (puladas) ··· {stats['travadas']}")
    if stats.get("sem_peca"):
        _log(log, f"Sem peça no app ··· {stats['sem_peca']}")
    _log(log, f"[saldo] novas=0 atualizadas=0 vendidas=0 imagens={imagens} vistos={cont['FOTO']}")
    _log(log, f"[progress] {ja + processadas} {max(1, total)} concluído")
    # nada capturado (sem ter parado e com peça pra fazer) = a conta não conseguiu → o agente tenta outra
    ok = parado or (total - ja) == 0 or cont["FOTO"] > 0
    return {"ok": ok, "motivo": "parado" if parado else ("ok" if ok else "nenhuma foto capturada"),
            "vistos": cont["FOTO"], "enviados": enviadas, "novas": 0, "atualizadas": 0,
            "vendidas": 0, "imagens": imagens, "stats": stats}


# ════════════════════════════ ENTRYPOINT ════════════════════════════
def run(d, params, log=None, parar=None):
    """Entrypoint chamado pelo agente (agente_emulador.py). `d` é o cliente uiautomator2 JÁ criado
    pelo agente (um por instância) — este módulo NÃO cria outro.

    params (vêm do job enfileirado pelo backend do brechó):
      - brecho_base   (str, OBRIGATÓRIO): base pública do backend do brechó (ex.: .../brecho)
      - brecho_token  (str, OBRIGATÓRIO): token Bearer do backend do brechó
      - run_id        (str): id da run (vai no corpo do POST)
      - full          (bool, opcional): fase A lê TODOS os posts (não para no 1º conhecido)
      - limite        (int, opcional): cap de segurança de posts da fase A (default LIMITE_PADRAO)
      - navegacao     (str, opcional): "celula" (padrão) | "auto" | "swipe"
      - modo          (str, opcional): "fotos" = só RECAPTURA a foto de todas as peças com link
                      (recapturar_fotos) — não lê legenda nem mexe em outro dado da peça
    (os alvos — disponiveis/recentes — NÃO vêm mais nos params: são buscados no GET /alvos.)

    Fluxo: conta_segura → buscar_alvos → FASE A (posts novos) → FASE B (conferir vendidas) →
    enviar_posts (fase A + B juntas, done=true no último lote). No modo "fotos": conta_segura →
    recapturar_fotos.
    Devolve {"ok", "motivo", "vistos", "enviados", "novas", "atualizadas", "vendidas", "imagens",
    "stats"}. Emite [progress]/[saldo] no `log`. NUNCA raspa logado na conta da loja."""
    params = params or {}
    parar = parar or (lambda: False)
    full = bool(params.get("full"))
    limite = int(params.get("limite") or LIMITE_PADRAO)
    navegacao = str(params.get("navegacao") or NAVEGACAO_PADRAO)
    brecho_base = params.get("brecho_base")
    brecho_token = params.get("brecho_token")
    if not brecho_base or not brecho_token:
        raise ScraperAbort("brecho_base/brecho_token ausentes nos params do job — não sei pra onde "
                           "falar com o backend")
    _debug_fotos["n"] = 0                   # debug de calibração: as 3 primeiras fotos DESTA run

    # guarda de segurança ANTES de qualquer coisa
    conta_segura(d, log)

    if str(params.get("modo") or "").strip().lower() == "fotos":
        return recapturar_fotos(d, params, log, parar)

    # alvos do backend: disponiveis (conferir venda) + recentes (boundary dos posts novos)
    alvos = buscar_alvos(brecho_base, brecho_token, log)
    disponiveis = alvos.get("disponiveis") or []
    recentes = alvos.get("recentes") or []
    known_codes = set(recentes) | {a.get("code") for a in disponiveis if a.get("code")}
    known_nums = {int(a["num"]) for a in disponiveis if a.get("num") is not None}
    sem_link = [int(n) for n in (alvos.get("sem_link") or []) if n is not None]
    conhecidos = {int(n) for n in (alvos.get("conhecidos") or []) if n is not None}
    total = len(disponiveis)            # as sem-link achadas entram em `novos` (não somar 2x)

    # envio AOS POUCOS: peças novas logo depois da fase A e a fase B a cada 10 peças — o app vai se
    # atualizando durante a run e uma queda no meio não perde o que já foi conferido. Os números e os
    # eventos de cada lote somam aqui pro "O que mudou" e o saldo do fim.
    stats, eventos, enviados_tot = {}, [], [0]

    def _enviar(posts, done=False):
        n_env, st = enviar_posts(posts, brecho_base=brecho_base, brecho_token=brecho_token,
                                 run_id=params.get("run_id"), full=full, done=done, log=log)
        enviados_tot[0] += n_env
        eventos.extend(st.pop("eventos", None) or [])
        _merge_stats(stats, st)

    # ── FASE A: posts NOVOS (para no 1º já conhecido; full lê todos). Se tem peça sem link de drop
    # publicado, desce atrás delas até o drop mais antigo que ainda tem uma à venda. ──
    abrir_perfil(d, PERFIL_LOJA, log)
    entrar_no_feed(d, log)
    _log(log, "─── Peças novas (topo do feed) ───")
    novos = raspar(d, known_codes=known_codes, known_nums=known_nums, sem_link=sem_link,
                   conhecidos=conhecidos, profundidade=alvos.get("profundidade_sem_link"),
                   full=full, limite=limite, navegacao=navegacao, log=log, parar=parar,
                   total_base=total)
    if novos:
        _enviar(novos)                  # peças novas já entram no app antes da fase B

    # ── FASE B: conferir VENDIDAS (abre cada disponível pelo link) ──
    total_geral = total + len(novos)
    _log(log, f"─── Conferindo as {len(disponiveis)} peças à venda ───")
    checadas = conferir_vendidas(d, disponiveis, log=log, parar=parar,
                                 feitos0=len(novos), total=total_geral, ao_lote=_enviar)
    _enviar([], done=True)              # fecha (lote vazio com done=true)
    stats["eventos"] = eventos

    # log PEÇA-A-PEÇA (igual ao scraper antigo): o backend devolve o que mudou em cada peça
    if eventos:
        _log(log, f"─── O que mudou ({len(eventos)}) ───")
        for ev in eventos:
            _log(log, ev)                   # sem recuo: o app colore pelo STATUS do começo

    vistos = len(novos) + len(checadas)
    novas = int(stats.get("novas") or 0)
    atualizadas = int(stats.get("atualizadas") or 0)
    vendidas = int(stats.get("recem_vendidas") or 0)
    imagens = int(stats.get("imagens") or 0)
    _log(log, f"[saldo] novas={novas} atualizadas={atualizadas} vendidas={vendidas} "
              f"imagens={imagens} vistos={vistos}")
    _log(log, f"[progress] {vistos} {max(1, total_geral)} concluído")
    return {"ok": True, "motivo": "ok", "vistos": vistos, "enviados": enviados_tot[0],
            "novas": novas, "atualizadas": atualizadas, "vendidas": vendidas,
            "imagens": imagens, "stats": stats}
