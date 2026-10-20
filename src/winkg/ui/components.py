"""Reusable Flet UI primitives for a consistent desktop operations console."""
from __future__ import annotations

import flet as ft
from .theme import ACCENT, BORDER, MUTED, PANEL, TEXT


def card(
    content: ft.Control,
    *,
    padding: int = 18,
    bgcolor: str = PANEL,
    width: int | None = None,
    border_color: str = BORDER,
    radius: int = 15,
) -> ft.Container:
    """A softly bordered surface with consistent shape and spacing."""
    return ft.Container(
        content=content,
        padding=padding,
        bgcolor=bgcolor,
        border=ft.Border.all(1, border_color),
        border_radius=radius,
        width=width,
    )


def section_title(title: str, subtitle: str | None = None) -> ft.Column:
    controls: list[ft.Control] = [
        ft.Text(title, size=18, weight=ft.FontWeight.W_600, color=TEXT)
    ]
    if subtitle:
        controls.append(ft.Text(subtitle, size=12, color=MUTED))
    return ft.Column(controls=controls, spacing=5)


def eyebrow(text: str, color: str = ACCENT) -> ft.Text:
    return ft.Text(text.upper(), size=9, weight=ft.FontWeight.BOLD, color=color)


def status_pill(text: str, color: str = ACCENT, *, icon: str | None = None) -> ft.Container:
    controls: list[ft.Control] = []
    if icon:
        controls.append(ft.Icon(icon, color=color, size=14))
    else:
        controls.append(ft.Container(width=6, height=6, bgcolor=color, border_radius=3))
    controls.append(ft.Text(text, size=10, weight=ft.FontWeight.W_600, color=color))
    return ft.Container(
        content=ft.Row(controls=controls, spacing=7, tight=True, vertical_alignment=ft.CrossAxisAlignment.CENTER),
        bgcolor="#142D32" if color == ACCENT else "#182538",
        border=ft.Border.all(1, color),
        border_radius=20,
        padding=ft.Padding.symmetric(horizontal=10, vertical=6),
    )


def icon_tile(icon: str, color: str = ACCENT, *, size: int = 38) -> ft.Container:
    return ft.Container(
        content=ft.Icon(icon, size=19, color=color),
        width=size,
        height=size,
        alignment=ft.Alignment(0, 0),
        bgcolor="#15383A" if color == ACCENT else "#1B2C40",
        border=ft.Border.all(1, color),
        border_radius=11,
    )
