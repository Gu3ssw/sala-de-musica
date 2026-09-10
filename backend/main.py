"""API de sala de música: uma fila partilhada alimentada pelo YouTube.

Um aparelho abre /player e toca. Os amigos abrem /remote no telemóvel e
acrescentam músicas. Tudo em tempo real por WebSocket.

    export YOUTUBE_API_KEY="a-tua-chave"
    uvicorn main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import string
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

log = logging.getLogger("sala")

API_KEY = os.getenv("YOUTUBE_API_KEY", "")
API_BASE = "https://www.googleapis.com/youtube/v3"
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "45"))

# Rede de segurança: 20 páginas de 50 são 1000 faixas. Impede que uma playlist
# sem fim (os Mix do YouTube) deixe o pedido a paginar para sempre.
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))

# Contas de quota: playlistItems.list e videos.list custam 1 unidade cada,
# mas search.list custa 100. Com 10 000 unidades por dia, são só 100 pesquisas.
# Por isso há um travão diário e o caminho preferido é colar o link.
SEARCH_BUDGET = int(os.getenv("SEARCH_BUDGET", "60"))

STATIC_DIR = Path(__file__).parent / "static"
UNPLAYABLE_TITLES = {"Deleted video", "Private video", "Vídeo eliminado", "Vídeo privado"}
ROOM_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem I, O, 0, 1


# --------------------------------------------------------------------------- #
# YouTube Data API
# --------------------------------------------------------------------------- #

def normalize_playlist_id(value: str) -> str:
    value = (value or "").strip()
    if value.startswith("http"):
        found = parse_qs(urlparse(value).query).get("list")
        value = found[0] if found else ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,64}", value):
        raise HTTPException(400, "ID de playlist inválido")
    if value.startswith("RD"):
        raise HTTPException(
            400,
            "Isso é um Mix ou rádio do YouTube: é gerado na hora e não tem fim, "
            "por isso a API nunca para de dar páginas. Usa uma playlist normal, "
            "com o ID a começar por PL.",
        )
    if value in {"LL", "WL"}:
        raise HTTPException(
            400,
            "As tuas 'Gostadas' e 'Ver mais tarde' são privadas, e uma chave de API não lhes chega.",
        )
    return value


def extract_video_id(value: str) -> str | None:
    """Aceita ID cru, youtu.be/…, /watch?v=…, /shorts/… ou /embed/…"""
    value = (value or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", value):
        return value
    if not value.startswith("http"):
        return None
    parsed = urlparse(value)
    if parsed.netloc.endswith("youtu.be"):
        candidate = parsed.path.lstrip("/").split("/")[0]
    elif "v" in parse_qs(parsed.query):
        candidate = parse_qs(parsed.query)["v"][0]
    else:
        match = re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{11})", parsed.path)
        candidate = match.group(1) if match else ""
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate) else None


async def youtube_get(path: str, params: dict) -> dict:
    if not API_KEY:
        raise RuntimeError("Falta a variável de ambiente YOUTUBE_API_KEY")
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.get(f"{API_BASE}/{path}", params={**params, "key": API_KEY})
    if response.status_code == 403:
        raise RuntimeError("Chave sem permissões ou quota diária esgotada")
    if response.status_code == 404:
        raise RuntimeError("Não encontrado no YouTube")
    log.info("youtube %s %s -> %s", path, {k: v for k, v in params.items() if k != "key"}, response.status_code)
    response.raise_for_status()
    return response.json()


def track_from_snippet(video_id: str, snippet: dict, source: str, added_by: str = "") -> dict:
    thumbs = snippet.get("thumbnails") or {}
    best = thumbs.get("medium") or thumbs.get("default") or {}
    return {
        "videoId": video_id,
        "title": snippet.get("title", ""),
        "channel": snippet.get("videoOwnerChannelTitle") or snippet.get("channelTitle", ""),
        "thumbnail": best.get("url", ""),
        "source": source,
        "addedBy": added_by,
        "addedAt": time.time(),
    }


async def fetch_playlist_items(playlist_id: str) -> list[dict]:
    tracks: list[dict] = []
    page_token: str | None = None
    pages = 0
    while True:
        params = {
            "part": "snippet,contentDetails",
            "playlistId": playlist_id,
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token
        payload = await youtube_get("playlistItems", params)
        for item in payload.get("items", []):
            snippet = item["snippet"]
            if snippet.get("title", "") in UNPLAYABLE_TITLES:
                continue
            tracks.append(
                track_from_snippet(item["contentDetails"]["videoId"], snippet, "playlist")
            )
        pages += 1
        log.info("playlist %s: página %s, %s faixas", playlist_id, pages, len(tracks))
        page_token = payload.get("nextPageToken")
        if not page_token or pages >= MAX_PAGES:
            break
    return tracks


async def fetch_video(video_id: str, added_by: str) -> dict:
    payload = await youtube_get("videos", {"part": "snippet,status", "id": video_id})
    items = payload.get("items", [])
    if not items:
        raise RuntimeError("Vídeo não encontrado ou indisponível")
    item = items[0]
    if not item.get("status", {}).get("embeddable", True):
        raise RuntimeError("O dono deste vídeo desativou a reprodução fora do YouTube")
    return track_from_snippet(video_id, item["snippet"], "added", added_by)


class SearchBudget:
    """Impede que meia dúzia de pesquisas queime a quota do dia todo."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.day = time.gmtime().tm_yday

    def spend(self) -> None:
        today = time.gmtime().tm_yday
        if today != self.day:
            self.day, self.used = today, 0
        if self.used >= self.limit:
            raise RuntimeError(
                "Pesquisas esgotadas por hoje. Cola o link do YouTube, que não gasta quota."
            )
        self.used += 1

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)


search_budget = SearchBudget(SEARCH_BUDGET)


async def search_videos(query: str, limit: int = 8) -> list[dict]:
    search_budget.spend()
    payload = await youtube_get(
        "search",
        {
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoEmbeddable": "true",
            "videoCategoryId": "10",  # música
            "maxResults": min(limit, 15),
        },
    )
    return [
        track_from_snippet(item["id"]["videoId"], item["snippet"], "search")
        for item in payload.get("items", [])
        if item.get("id", {}).get("videoId")
    ]


# --------------------------------------------------------------------------- #
# Sala
# --------------------------------------------------------------------------- #

def clean_name(value: object) -> str:
    """Nomes vêm do cliente, por isso são cortados e limpos antes de circular."""
    text = " ".join(str(value or "").split())
    return text[:20]


class Member:
    """Quem está ligado à sala: o aparelho que toca, ou uma pessoa."""

    __slots__ = ("role", "name")

    def __init__(self, role: str, name: str = "") -> None:
        self.role = role
        self.name = name


class Room:
    """Estado partilhado de uma sala.

    O aparelho que toca é a fonte da verdade sobre a posição atual: reporta
    'nowPlaying' e o servidor reencaminha para os telemóveis. Os telemóveis
    mandam comandos, que o servidor reencaminha para o aparelho.
    """

    def __init__(self, room_id: str, playlist_id: str | None = None) -> None:
        self.id = room_id
        self.playlist_id = playlist_id
        self.queue: list[dict] = []
        self.index = 0
        self.playing = False
        self.position = 0.0  # segundos dentro da faixa, reportados pelo aparelho
        self.position_at = time.time()
        self.notice: str | None = None
        self.revision = 0
        self.clients: dict[WebSocket, Member] = {}
        self.touched_at = time.time()
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    # -- estado ------------------------------------------------------------ #

    def state(self) -> dict:
        return {
            "type": "state",
            "roomId": self.id,
            "playlistId": self.playlist_id,
            "revision": self.revision,
            "index": self.index,
            "playing": self.playing,
            "position": self.position,
            "positionAt": self.position_at,
            "queue": self.queue,
            "notice": self.notice,
            "hasPlayer": any(member.role == "player" for member in self.clients.values()),
            "listeners": sum(1 for member in self.clients.values() if member.role == "remote"),
            "people": [
                member.name or "anónimo"
                for member in self.clients.values()
                if member.role == "remote"
            ],
            "searchesLeft": search_budget.remaining,
        }

    def bump(self) -> None:
        self.revision += 1
        self.touched_at = time.time()

    async def broadcast(self, payload: dict | None = None, only: str | None = None) -> None:
        message = payload or self.state()
        for client, member in list(self.clients.items()):
            if only and member.role != only:
                continue
            try:
                await client.send_json(message)
            except Exception:
                self.clients.pop(client, None)

    # -- fila -------------------------------------------------------------- #

    def current_video_id(self) -> str | None:
        return self.queue[self.index]["videoId"] if 0 <= self.index < len(self.queue) else None

    async def add(self, track: dict) -> str:
        if any(item["videoId"] == track["videoId"] for item in self.queue):
            return "Essa já está na fila."
        self.queue.append(track)
        self.bump()
        await self.broadcast()
        return f"Adicionada: {track['title']}"

    async def remove(self, video_id: str) -> None:
        if video_id == self.current_video_id():
            return  # não tiramos o chão a quem está a tocar
        keep_id = self.current_video_id()
        self.queue = [item for item in self.queue if item["videoId"] != video_id]
        if keep_id:
            self.index = next(
                (i for i, item in enumerate(self.queue) if item["videoId"] == keep_id), 0
            )
        self.bump()
        await self.broadcast()

    async def shuffle(self) -> None:
        """Embaralha o que falta, sem mexer no que já passou nem na atual."""
        head = self.queue[: self.index + 1]
        tail = self.queue[self.index + 1 :]
        random.shuffle(tail)
        self.queue = head + tail
        self.bump()
        await self.broadcast()

    def sync_playlist(self, tracks: list[dict]) -> bool:
        """Alinha a fila com a playlist do YouTube, poupando as adições dos amigos."""
        incoming = {track["videoId"]: track for track in tracks}
        current = self.current_video_id()
        changed = False

        if not self.queue:
            self.queue = list(tracks)
            return bool(tracks)

        # Saíram da playlist -> saem da fila (exceto a que está a tocar).
        surviving = [
            item
            for item in self.queue
            if item["source"] != "playlist"
            or item["videoId"] in incoming
            or item["videoId"] == current
        ]
        if len(surviving) != len(self.queue):
            self.queue, changed = surviving, True

        # Novas na playlist -> vão para o fim da fila.
        known = {item["videoId"] for item in self.queue}
        for track in tracks:
            if track["videoId"] not in known:
                self.queue.append(track)
                changed = True

        if changed and current:
            self.index = next(
                (i for i, item in enumerate(self.queue) if item["videoId"] == current), self.index
            )
        return changed

    # -- polling da playlist ----------------------------------------------- #

    async def refresh_playlist(self) -> bool:
        if not self.playlist_id:
            return False
        async with self._lock:
            try:
                tracks = await fetch_playlist_items(self.playlist_id)
            except Exception as exc:
                previous, self.notice = self.notice, str(exc)
                return previous != self.notice
            self.notice = None
            if self.sync_playlist(tracks):
                self.bump()
                return True
            return False

    async def ensure_started(self) -> None:
        if self.playlist_id and self.revision == 0 and self.notice is None:
            await self.refresh_playlist()
        if self.playlist_id and (self._task is None or self._task.done()):
            self._task = asyncio.create_task(self._poll_forever())

    async def _poll_forever(self) -> None:
        while self.clients:
            await asyncio.sleep(POLL_SECONDS)
            if await self.refresh_playlist():
                await self.broadcast()

    def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None


rooms: dict[str, Room] = {}


def new_room_code() -> str:
    while True:
        code = "".join(random.choices(ROOM_ALPHABET, k=4))
        if code not in rooms:
            return code


def get_room(room_id: str) -> Room:
    room = rooms.get(room_id.upper())
    if room is None:
        raise HTTPException(404, "Sala não encontrada. Cria uma nova.")
    return room


# --------------------------------------------------------------------------- #
# Rotas
# --------------------------------------------------------------------------- #

app = FastAPI(title="Sala de música")

app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)

if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class CreateRoom(BaseModel):
    playlist: str | None = None


@app.get("/")
async def root():
    return _static_file("start.html")


@app.get("/p/{room_id}")
async def player_short(room_id: str):
    """Endereço curto do aparelho que toca: /p/ABCD"""
    return _static_file("player.html")


@app.get("/r/{room_id}")
async def remote_short(room_id: str):
    """Endereço curto para os amigos: /r/ABCD — dá um QR code mais simples."""
    return _static_file("remote.html")


@app.get("/health")
async def health():
    return {
        "ok": True,
        "keyConfigured": bool(API_KEY),
        "rooms": len(rooms),
        "searchesLeft": search_budget.remaining,
    }


@app.post("/api/rooms")
async def create_room(body: CreateRoom):
    playlist_id = normalize_playlist_id(body.playlist) if body.playlist else None
    room = Room(new_room_code(), playlist_id)
    rooms[room.id] = room
    if playlist_id:
        await room.refresh_playlist()
        if room.notice and not room.queue:
            del rooms[room.id]
            raise HTTPException(502, room.notice)
    return {"roomId": room.id, "playlistId": playlist_id, "tracks": len(room.queue)}


@app.get("/api/rooms/{room_id}")
async def read_room(room_id: str):
    room = get_room(room_id)
    await room.ensure_started()
    return room.state()


@app.get("/api/search")
async def search(q: str):
    if len(q.strip()) < 2:
        raise HTTPException(400, "Escreve pelo menos duas letras")
    try:
        return {"results": await search_videos(q), "searchesLeft": search_budget.remaining}
    except RuntimeError as exc:
        raise HTTPException(429, str(exc))


@app.get("/player")
async def player_page():
    return _static_file("player.html")


@app.get("/remote")
async def remote_page():
    return _static_file("remote.html")


def _static_file(name: str) -> FileResponse:
    page = STATIC_DIR / name
    if not page.is_file():
        raise HTTPException(404, f"{name} não encontrado")
    return FileResponse(page)


@app.websocket("/ws/room/{room_id}")
async def room_socket(websocket: WebSocket, room_id: str):
    await websocket.accept()
    role = websocket.query_params.get("role", "remote")
    role = "player" if role == "player" else "remote"

    room = rooms.get(room_id.upper())
    if room is None:
        # O servidor reinicia a cada deploy e as salas vivem em memória. Em vez
        # de deixar toda a gente em ciclo de reconexão, a sala é recriada com o
        # mesmo código e o aparelho que toca repõe a fila logo a seguir.
        code = room_id.upper()
        if not re.fullmatch(r"[A-Z0-9]{4}", code):
            await websocket.send_json({"type": "error", "message": "Código de sala inválido"})
            await websocket.close()
            return
        room = Room(code)
        rooms[code] = room

    member = Member(role, clean_name(websocket.query_params.get("name")))
    if role == "player" and not member.name:
        member.name = "o dono da sala"
    room.clients[websocket] = member
    try:
        await room.ensure_started()
        await websocket.send_json(room.state())
        await room.broadcast()  # os outros passam a ver que entrou alguém
        while True:
            await handle_message(room, member, await websocket.receive_json())
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # JSON inválido, cliente estranho
        await websocket.send_json({"type": "error", "message": str(exc)})
    finally:
        room.clients.pop(websocket, None)
        if room.clients:
            await room.broadcast()
        else:
            room.stop()


async def handle_message(room: Room, member: Member, message: dict) -> None:
    kind = message.get("type")
    role = member.role

    if kind == "hello":  # a pessoa apresenta-se ao entrar
        member.name = clean_name(message.get("name"))
        await room.broadcast()
        return

    if kind == "add":
        video_id = extract_video_id(message.get("value", ""))
        if not video_id:
            await room.broadcast({"type": "toast", "message": "Esse link não parece do YouTube."})
            return
        try:
            track = await fetch_video(video_id, member.name or "anónimo")
        except Exception as exc:
            await room.broadcast({"type": "toast", "message": str(exc)})
            return
        await room.broadcast({"type": "toast", "message": await room.add(track)})

    elif kind == "addTrack":  # vem da pesquisa, já traz os metadados
        track = message.get("track") or {}
        if extract_video_id(track.get("videoId", "")):
            track["source"] = "added"
            track["addedBy"] = member.name or "anónimo"
            await room.broadcast({"type": "toast", "message": await room.add(track)})

    elif kind == "setPlaylist":
        try:
            playlist_id = normalize_playlist_id(message.get("value", ""))
        except HTTPException as exc:
            await room.broadcast({"type": "toast", "message": exc.detail})
            return
        room.playlist_id = playlist_id
        room.notice = None
        await room.refresh_playlist()
        await room.ensure_started()
        await room.broadcast(
            {
                "type": "toast",
                "message": room.notice or f"Playlist ligada: {len(room.queue)} faixas na fila.",
            }
        )
        await room.broadcast()

    elif kind == "restore" and role == "player":
        # Só repõe uma sala vazia: nunca sobrepõe o que já lá estiver.
        if room.queue:
            return
        restored: list[dict] = []
        for item in (message.get("tracks") or [])[:500]:
            video_id = extract_video_id(str(item.get("videoId", "")))
            if not video_id:
                continue
            restored.append(
                {
                    "videoId": video_id,
                    "title": str(item.get("title", ""))[:200],
                    "channel": str(item.get("channel", ""))[:120],
                    "thumbnail": str(item.get("thumbnail", ""))[:400],
                    "source": "playlist" if item.get("source") == "playlist" else "added",
                    "addedBy": str(item.get("addedBy", ""))[:40],
                    "addedAt": time.time(),
                }
            )
        if not restored:
            return
        room.queue = restored
        playlist = message.get("playlist")
        if playlist:
            try:
                room.playlist_id = normalize_playlist_id(str(playlist))
            except HTTPException:
                pass
        room.bump()
        await room.ensure_started()
        log.info("sala %s reposta com %s faixas", room.id, len(restored))
        await room.broadcast(
            {"type": "toast", "message": "O servidor reiniciou. Fila reposta."}
        )
        await room.broadcast()

    elif kind == "remove":
        await room.remove(message.get("videoId", ""))

    elif kind == "shuffle":
        await room.shuffle()

    elif kind == "command":  # telemóvel -> aparelho que toca
        action = message.get("action")
        if action in {"next", "prev", "play", "pause", "playIndex"}:
            await room.broadcast(
                {"type": "command", "action": action, "index": message.get("index")},
                only="player",
            )

    elif kind == "nowPlaying" and role == "player":  # aparelho -> telemóveis
        index = message.get("index")
        if isinstance(index, int) and 0 <= index < len(room.queue):
            room.index = index
        room.playing = bool(message.get("playing"))
        room.position = float(message.get("seconds") or 0)
        room.position_at = time.time()
        room.touched_at = time.time()
        await room.broadcast()

    elif kind == "tick" and role == "player":
        # Batida de relógio a cada poucos segundos, só para os telemóveis que
        # escolheram ouvir localmente se manterem alinhados. Não leva a fila
        # inteira atrás, que seria desperdício.
        index = message.get("index")
        if isinstance(index, int) and 0 <= index < len(room.queue):
            room.index = index
        room.playing = bool(message.get("playing"))
        room.position = float(message.get("seconds") or 0)
        room.position_at = time.time()
        room.touched_at = time.time()
        await room.broadcast(
            {
                "type": "tick",
                "index": room.index,
                "playing": room.playing,
                "position": room.position,
                "videoId": room.current_video_id(),
            },
            only="remote",
        )
