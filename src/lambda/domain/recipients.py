"""Resolución de destinatarios según listas de distribución con nombre.

Reglas puras (sin I/O) reutilizables por Telegram (el Lambda conoce los contactos)
y por WhatsApp (el servicio aplica la misma lógica sobre sus propios contactos).

Modos de targeting (`target.mode`):
- "all":    enviar a todos (salvo los siempre-excluidos).
- "only":   enviar SOLO a los miembros de las listas activas (whitelist).
- "except": enviar a todos EXCEPTO a los miembros de las listas activas (blacklist).

Estructuras:
- lists:  [{"name": str, "ids": [str]}]
- target: {"mode": "all"|"only"|"except", "lists": [str]}  (lists = nombres activos)
"""

from __future__ import annotations

from collections.abc import Iterable


def ids_de_listas_activas(lists: Iterable[dict], target: dict) -> set[str]:
    """Unión (como strings) de los ids de las listas cuyo nombre está en target['lists']."""
    activas = set(target.get("lists", []) or [])
    seleccion: set[str] = set()
    for lista in lists or []:
        if (lista or {}).get("name") in activas:
            seleccion.update(str(x) for x in (lista or {}).get("ids", []))
    return seleccion


def filtrar_destinatarios(
    todos: Iterable,
    lists: Iterable[dict],
    target: dict,
    *,
    excluidos: Iterable = (),
) -> list:
    """Devuelve el subconjunto de `todos` que debe recibir el envío.

    Conserva el tipo y orden originales de `todos`. Los `excluidos` se quitan siempre,
    en cualquier modo. En modo "only", un id de lista que no esté en `todos` se ignora
    (no se puede enviar a quien no es contacto).
    """
    seleccion = ids_de_listas_activas(lists, target)
    excl = {str(x) for x in (excluidos or [])}
    mode = (target or {}).get("mode", "all")
    salida = []
    for c in todos:
        s = str(c)
        if s in excl:
            continue
        if mode == "only" and s not in seleccion:
            continue
        if mode == "except" and s in seleccion:
            continue
        salida.append(c)
    return salida
