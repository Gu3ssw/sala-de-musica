"""App de telemóvel em Flet para a sala de música.

Duas utilizações: ser o aparelho que toca, ou entrar numa sala para pôr músicas.
A app é uma casca com WebView — as páginas vivem no servidor, por isso podes
mudar o player sem reconstruir o APK.

    flet run main.py            # no computador
    flet run --android main.py  # no telemóvel, pela app Flet
    flet build apk              # APK (precisa do Flutter SDK)
"""

from __future__ import annotations

import re

import flet as ft

# 10.0.2.2 é como o emulador Android chega ao localhost da tua máquina.
# Num telemóvel real, mete o IP da máquina: http://192.168.1.70:8000
DEFAULT_API = "http://10.0.2.2:8000"

BASE = "#0e1626"
SURFACE = "#172038"
LINE = "#26314f"
TEXT = "#e8eaf2"
MUTED = "#8b95b4"
ACCENT = "#f0a94c"


def main(page: ft.Page):
    page.title = "Sala de música"
    page.bgcolor = BASE
    page.padding = 0
    page.theme_mode = ft.ThemeMode.DARK

    api_field = ft.TextField(
        label="Endereço do servidor",
        value=page.client_storage.get("api") or DEFAULT_API,
        border_color=LINE,
        focused_border_color=ACCENT,
        color=TEXT,
        keyboard_type=ft.KeyboardType.URL,
    )
    code_field = ft.TextField(
        label="Código da sala",
        hint_text="ABCD",
        max_length=4,
        capitalization=ft.TextCapitalization.CHARACTERS,
        border_color=LINE,
        focused_border_color=ACCENT,
        color=TEXT,
    )
    error = ft.Text("", color=ACCENT, size=13)

    def api_base() -> str | None:
        value = (api_field.value or "").strip().rstrip("/")
        return value if value.startswith("http") else None

    def open_url(url: str):
        if not hasattr(ft, "WebView"):
            error.value = "Esta versão do Flet não traz WebView. Atualiza: pip install -U flet"
            page.update()
            return
        page.controls.clear()
        page.add(
            ft.Column(
                spacing=0,
                expand=True,
                controls=[
                    ft.Container(
                        bgcolor=SURFACE,
                        padding=ft.padding.symmetric(4, 6),
                        content=ft.Row(
                            controls=[
                                ft.IconButton(
                                    ft.Icons.ARROW_BACK,
                                    icon_color=TEXT,
                                    tooltip="Voltar",
                                    on_click=lambda _: show_home(),
                                ),
                                ft.Text(url.split("//", 1)[-1], size=11, color=MUTED),
                            ]
                        ),
                    ),
                    ft.WebView(url=url, expand=True),
                ],
            )
        )
        page.update()

    def host_room(_):
        base = api_base()
        if not base:
            error.value = "O endereço tem de começar por http:// ou https://."
            page.update()
            return
        page.client_storage.set("api", base)
        # A página inicial abre a sala e salta para o player.
        open_url(f"{base}/static/start.html")

    def join_room(_):
        base = api_base()
        code = (code_field.value or "").strip().upper()
        if not base:
            error.value = "O endereço tem de começar por http:// ou https://."
        elif not re.fullmatch(r"[A-Z0-9]{4}", code):
            error.value = "O código são quatro letras ou números."
        else:
            error.value = ""
            page.client_storage.set("api", base)
            open_url(f"{base}/remote?room={code}")
        page.update()

    def show_home():
        page.controls.clear()
        page.add(
            ft.Container(
                padding=24,
                content=ft.Column(
                    spacing=14,
                    controls=[
                        ft.Text("Sala de música", size=22, weight=ft.FontWeight.W_600, color=TEXT),
                        ft.Text(
                            "Um aparelho toca com o ecrã aceso. Os outros entram pelo código "
                            "e vão pondo músicas na fila.",
                            size=13,
                            color=MUTED,
                        ),
                        api_field,
                        ft.ElevatedButton(
                            "Tocar neste aparelho",
                            bgcolor=ACCENT,
                            color="#241703",
                            width=10_000,
                            height=46,
                            on_click=host_room,
                        ),
                        ft.Divider(color=LINE),
                        code_field,
                        ft.OutlinedButton(
                            "Entrar e pôr músicas",
                            width=10_000,
                            height=46,
                            on_click=join_room,
                        ),
                        error,
                    ],
                ),
            )
        )
        page.update()

    show_home()


ft.app(main)
