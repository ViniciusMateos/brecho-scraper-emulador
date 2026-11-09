# Changelog

## [1.0.0] — 2026-11-09

Primeira versão no repo. O módulo roda no emulador desde 21/10/2026; aqui entra o estado atual.

### Adicionado
- feat: raspagem do perfil do brechó pelo app real do IG no emulador, numa conta auxiliar (a conta da loja nunca é usada)
- feat: fase A lê os posts novos do topo até o primeiro conhecido e desce atrás das peças sem link até a profundidade do drop mais antigo
- feat: fase B abre cada peça disponível pelo link, lê a legenda e tira a foto, com o resultado ao vivo no log (VENDIDA, DISPONÍVEL, ATUALIZADA com o preço novo)
- feat: envio em lotes de 10 (o app atualiza durante a run)
- feat: modo `fotos` recaptura a foto de toda peça com link sem mexer nos dados
- feat: foto limpa dos overlays do IG (fileira de repost, contador do carrossel, ícones) com corte da borda

### Corrigido
- fix: fileira de repost com 3 avatares (ou 2 + a dica "Partilha um emoji…") sobrava na foto; agora sai sempre, preservando alça e cadarço da peça que passam perto
- fix: toque no "… mais" de legenda de uma linha abria o perfil e lia o perfil como legenda
