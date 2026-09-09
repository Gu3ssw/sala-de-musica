# Sala de música — fila partilhada com o YouTube

Um aparelho toca com o ecrã aceso. Os amigos entram por link no telemóvel e vão
pondo músicas na fila. Playback pelo player oficial do YouTube, sem contornar nada.

- `backend/` — FastAPI: salas, fila partilhada, sincronização com uma playlist do YouTube, WebSocket.
- `backend/static/` — as três páginas: abrir/entrar, aparelho que toca, telemóvel dos amigos.
- `mobile/` — app Flet opcional, casca com WebView para não andares a colar links.

## 1. Chave da API

Google Cloud → novo projeto → ativa **YouTube Data API v3** → cria uma credencial
do tipo **API key**. A playlist de arranque tem de ser pública ou não listada.

## 2. Correr

```bash
cd backend
pip install -r requirements.txt
export YOUTUBE_API_KEY="a-tua-chave"     # Windows: set YOUTUBE_API_KEY=...
uvicorn main:app --host 0.0.0.0 --port 8000
```

Abre `http://localhost:8000` no aparelho que vai tocar, cola a playlist (ou deixa
vazio) e carrega em abrir sala. Aparece um código de quatro letras e um QR code.
Os amigos leem o QR ou vão a `http://<o-teu-ip>:8000` e metem o código.

## 3. Como funciona a fila

- A playlist do YouTube é sincronizada a cada 45 segundos. Se editares a playlist,
  as novas entram no fim da fila e as removidas saem — exceto a que está a tocar.
- O que os amigos acrescentam nunca é apagado pela sincronização.
- O aparelho que toca é a fonte da verdade sobre a posição atual. Os telemóveis
  mandam comandos (seguinte, pausa, tocar esta) que o servidor reencaminha.
- Duplicados são recusados, e ninguém pode tirar da fila a música que está a tocar.

## 4. Ecrã aceso

A página do player pede um **Screen Wake Lock** quando começa a tocar, o que
impede o ecrã de bloquear. Só funciona em contexto seguro: `localhost` ou HTTPS.
Em HTTP na rede local, o pedido falha e a página avisa-te — nesse caso põe o
tempo de bloqueio no máximo nas definições do aparelho.

Para teres HTTPS na rede local sem chatices, corre o servidor atrás de um túnel
(`cloudflared tunnel --url http://localhost:8000`) e usa o endereço que ele dá.
Ganhas o wake lock e os amigos podem entrar de fora de casa.

## 5. Quota

10 000 unidades por dia. Custos por chamada:

| Ação | Custo | Notas |
|---|---|---|
| Sincronizar a playlist | 1 | a cada 45 s dá ~1 900/dia por sala |
| Colar um link | 1 | caminho recomendado |
| Pesquisar pelo nome | **100** | limitado a 60/dia por `SEARCH_BUDGET` |

Por isso a página dos amigos põe o campo de colar link primeiro e mostra quantas
pesquisas restam. Se precisares de mais, sobe `SEARCH_BUDGET` e o polling
(`POLL_SECONDS`) em conjunto, ou pede aumento de quota à Google.

## 6. App de telemóvel (opcional)

```bash
cd mobile
pip install -r requirements.txt
flet run main.py
flet build apk
```

Android bloqueia HTTP simples em builds de release: se gerares APK, serve o
backend por HTTPS.

## O que isto não faz

Não toca com o ecrã bloqueado. O player IFrame do YouTube para quando a app vai
para background, e a subscrição Premium não se aplica dentro de um embed. Se um
dia quiseres áudio de bolso, o caminho é trocar a fonte por um catálogo com
streaming licenciado por API (Audius, Jamendo) e escrever a camada nativa de
áudio — o backend de salas aproveita-se quase todo.
