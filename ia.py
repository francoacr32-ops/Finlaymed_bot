"""Respuestas redactadas con IA (opcional).

Solo se usa si existe la variable ANTHROPIC_API_KEY. La IA recibe los fragmentos
que encontró el buscador y redacta la respuesta únicamente con ellos, citando
libro y página. Sin clave, el bot responde con los fragmentos tal cual.
"""

import logging
import os

import anthropic

from biblioteca import ASPECTOS, Resultado

registro = logging.getLogger(__name__)

MODELO = os.environ.get("MODELO_IA", "claude-opus-5-5")

# Modelos que admiten el modelo de respaldo automático del servidor.
_CON_RESPALDO = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-2", "claude-fable-5-1"}

SISTEMA = """Eres el tutor de Morfofisiología de estudiantes de Medicina cubanos. Respondes dentro de un chat de Telegram.

Te llegan una pregunta y fragmentos numerados de los libros de texto de la asignatura. Esos libros son la única fuente válida: el estudiante se examina por ellos, así que una respuesta correcta según otra bibliografía pero ausente del libro no le sirve.

- Responde solo con lo que dicen los fragmentos. Después de cada idea indica la fuente entre paréntesis, tal como aparece en el encabezado del fragmento, por ejemplo: (Morfofisiología I, pág. 45).
- Si los fragmentos no contienen la respuesta, dilo con la frase "No encontré eso en los libros" y, si algo de lo que traen se acerca, menciónalo. No completes con conocimiento propio.
- Si traen solo una parte de lo que se pregunta, responde esa parte y di qué faltó.
- Cuando se pregunta por una estructura sin precisar, organiza lo que haya en este orden: situación y anatomía, relaciones, irrigación e inervación, histología, embriología, fisiología. Omite los apartados de los que no haya nada.
- Antes de tu respuesta el estudiante recibe, como fotos, las figuras que el libro trae en esas mismas páginas. No las ves, así que no describas su contenido; sí puedes remitir a "la figura de la pág. N" cuando el fragmento la mencione.
- Usa la terminología de los libros. Escribe en español, en texto plano: Telegram no muestra Markdown, así que no uses asteriscos, almohadillas ni tablas; separa los apartados con una línea en blanco y un título corto en mayúsculas.
- Sé breve: lo que un estudiante necesita para repasar, normalmente menos de 300 palabras, o hasta 500 cuando se pide una estructura completa con todos sus apartados."""


def disponible() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


class Tutor:
    def __init__(self):
        self._cliente = anthropic.AsyncAnthropic()

    async def responder(self, pregunta: str, aspecto: str | None, resultados: list[Resultado]) -> str | None:
        """Devuelve la respuesta redactada, o None si la IA no pudo responder."""
        fragmentos = "\n\n".join(f"[{i}] {r.fuente}\n{r.texto}" for i, r in enumerate(resultados, 1))
        enfoque = f"\nEl estudiante quiere específicamente: {ASPECTOS[aspecto][0]}." if aspecto in ASPECTOS else ""
        mensaje = f"Fragmentos de los libros:\n\n{fragmentos}\n\nPregunta del estudiante: {pregunta}{enfoque}"
        extras = {}
        if MODELO in _CON_RESPALDO:
            extras = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        try:
            respuesta = await self._cliente.beta.messages.create(
                model=MODELO,
                max_tokens=16000,
                system=SISTEMA,
                messages=[{"role": "user", "content": mensaje}],
                **extras,
            )
        except anthropic.RateLimitError:
            registro.warning("IA: límite de peticiones alcanzado")
            return None
        except anthropic.APIStatusError as error:
            registro.error("IA: error %s: %s", error.status_code, error.message)
            return None
        except anthropic.APIConnectionError:
            registro.error("IA: sin conexión con la API")
            return None
        if respuesta.stop_reason == "refusal":
            registro.warning("IA: la petición fue rechazada")
            return None
        texto = "".join(bloque.text for bloque in respuesta.content if bloque.type == "text").strip()
        return texto or None
