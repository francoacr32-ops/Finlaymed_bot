"""Finlaymed_bot: bot de Telegram que responde por los libros de Morfofisiología.

Variables de entorno:
  TELEGRAM_TOKEN     (obligatoria) el token que da @BotFather.
  ADMIN_IDS          ids de Telegram de quienes pueden cargar y borrar libros, separados por coma.
  ANTHROPIC_API_KEY  (opcional) activa las respuestas redactadas con IA.
  MODELO_IA          (opcional) modelo de IA; por defecto claude-opus-5-5.
  LIMITE_DIARIO      (opcional) respuestas con IA por persona y día; por defecto 30.
  DATA_DIR           (opcional) carpeta donde se guarda la biblioteca; por defecto ./datos.
  LIBROS_DIR         (opcional) carpeta con libros que se cargan al arrancar; por defecto ./libros.
  LIBROS_URLS        (opcional) enlaces directos a libros, separados por coma, que se descargan al arrancar.
  PORT               (opcional) si existe, abre una página mínima en ese puerto (lo piden algunos servidores).
"""

import asyncio
import datetime
import logging
import os
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

import ia
from biblioteca import ASPECTOS, Biblioteca, Resultado, es_estructura, leer_archivo

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
registro = logging.getLogger("finlaymed")

DATA_DIR = Path(os.environ.get("DATA_DIR", "datos"))
LIBROS_DIR = Path(os.environ.get("LIBROS_DIR", "libros"))
ADMINS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x.lstrip("-").isdigit()}
LIMITE_DIARIO = int(os.environ.get("LIMITE_DIARIO", "30"))
MAX_TELEGRAM = 20 * 1024 * 1024  # un bot solo puede descargar archivos de hasta 20 MB
NO_ENCONTRE = "No encontré eso en los libros. Prueba con otras palabras o con el nombre exacto de la estructura."

DATA_DIR.mkdir(parents=True, exist_ok=True)
biblioteca = Biblioteca(str(DATA_DIR / "biblioteca.db"))
tutor = ia.Tutor() if ia.disponible() else None
_usos: dict[tuple[int, datetime.date], int] = {}

AYUDA = """Soy el bot de Morfofisiología. Respondo con lo que dicen los libros de Morfofisiología I, II y III, y te digo de qué libro y página sale.

Escríbeme el nombre de una estructura o una pregunta. Por ejemplo:
• riñón
• ¿Cuáles son las relaciones del estómago?
• origen embriológico del corazón

Si escribes solo la estructura (por ejemplo: órgano de la visión) te envío las figuras del libro y, debajo, su anatomía, relaciones, histología, embriología y fisiología.

Debajo de cada respuesta hay botones para pedir un aspecto concreto de lo mismo. También puedes escribirlo directamente:
/anatomia hígado
/relaciones hígado
/histologia hígado
/embriologia hígado
/fisiologia hígado
/irrigacion hígado

/libros — libros que tengo cargados
/ayuda — este mensaje"""

AYUDA_ADMIN = """

Administrador:
• Envíame un libro como archivo (PDF, .txt de «Exportar texto» de Finlay, o paquete de Finlay .json/.zip) y lo agrego. Máximo 20 MB por archivo. Las figuras solo salen de los PDF.
• /borrar número — quita un libro (el número sale en /libros)."""


def es_admin(update: Update) -> bool:
    return update.effective_user is not None and update.effective_user.id in ADMINS


def botones() -> InlineKeyboardMarkup:
    claves = list(ASPECTOS)
    filas = [claves[:3], claves[3:]]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(ASPECTOS[c][0], callback_data=f"a:{c}") for c in fila] for fila in filas]
    )


def _cita(r: Resultado) -> str:
    texto = r.texto if len(r.texto) <= 900 else r.texto[:900].rsplit(" ", 1)[0] + "…"
    return f"📖 {r.fuente}\n{texto}"


def con_fragmentos(resultados: list[Resultado]) -> str:
    """Respuesta sin IA: los mejores fragmentos tal como están en el libro."""
    return "\n\n".join(_cita(r) for r in resultados[:3])


def con_secciones(secciones: list[tuple[str, Resultado]]) -> str:
    """Respuesta sin IA para una estructura: un apartado por aspecto."""
    return "\n\n".join(f"{ASPECTOS[a][0].upper()}\n{_cita(r)}" for a, r in secciones)


async def enviar_figuras(update: Update, figuras: list[tuple[str, bytes]]) -> None:
    try:
        if len(figuras) == 1:
            await update.effective_chat.send_photo(figuras[0][1], caption=f"📖 {figuras[0][0]}")
        elif figuras:
            await update.effective_chat.send_media_group(
                [InputMediaPhoto(datos, caption=f"📖 {fuente}") for fuente, datos in figuras]
            )
    except Exception:  # una figura que Telegram no acepta no debe dejar al estudiante sin respuesta
        registro.exception("No se pudieron enviar las figuras")


def puede_usar_ia(usuario: int) -> bool:
    hoy = datetime.date.today()
    for clave in [c for c in _usos if c[1] != hoy]:
        del _usos[clave]
    if _usos.get((usuario, hoy), 0) >= LIMITE_DIARIO:
        return False
    _usos[(usuario, hoy)] = _usos.get((usuario, hoy), 0) + 1
    return True


async def enviar(update: Update, texto: str, teclado: InlineKeyboardMarkup | None = None) -> None:
    """Envía texto largo en varios mensajes (Telegram admite 4096 caracteres por mensaje)."""
    trozos = []
    while len(texto) > 4000:
        corte = texto.rfind("\n", 0, 4000)
        corte = corte if corte > 2000 else 4000
        trozos.append(texto[:corte])
        texto = texto[corte:].lstrip()
    trozos.append(texto)
    for i, trozo in enumerate(trozos):
        ultimo = i == len(trozos) - 1
        await update.effective_chat.send_message(trozo, reply_markup=teclado if ultimo else None)


async def contestar(update: Update, context: ContextTypes.DEFAULT_TYPE, pregunta: str, aspecto: str | None) -> None:
    pregunta = pregunta.strip()
    if not pregunta:
        await update.effective_chat.send_message("Escribe la estructura después del comando. Ejemplo: /relaciones estómago")
        return
    if not biblioteca.libros():
        await update.effective_chat.send_message("Todavía no tengo libros cargados. El administrador debe enviármelos.")
        return
    context.user_data["ultima"] = pregunta
    await update.effective_chat.send_action(ChatAction.TYPING)
    resultados = await asyncio.to_thread(biblioteca.buscar, pregunta, 8, aspecto)
    if not resultados:
        await update.effective_chat.send_message(NO_ENCONTRE)
        return
    # Solo el nombre de una estructura: se responde con todos sus aspectos.
    secciones = []
    if aspecto is None and es_estructura(pregunta):
        secciones = await asyncio.to_thread(biblioteca.resumen, pregunta)
    if secciones:
        de_secciones = [r for _, r in secciones]
        textos = {r.texto for r in de_secciones}
        resultados = de_secciones + [r for r in resultados if r.texto not in textos][:4]
        mostrados = de_secciones
    else:
        mostrados = resultados[:3]
    respuesta = None
    if tutor and puede_usar_ia(update.effective_user.id):
        respuesta = await tutor.responder(pregunta, aspecto, resultados)
    await enviar_figuras(update, await asyncio.to_thread(biblioteca.figuras, mostrados))
    await enviar(update, respuesta or (con_secciones(secciones) if secciones else con_fragmentos(resultados)), botones())


# ---------- comandos ----------


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(AYUDA + (AYUDA_ADMIN if es_admin(update) else ""))


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_chat.send_message(f"Tu id de Telegram es {update.effective_user.id}")


async def cmd_libros(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    libros = biblioteca.libros()
    if not libros:
        await update.effective_chat.send_message("Todavía no tengo libros cargados.")
        return
    lineas = [f"{i}. {titulo} ({paginas} páginas, {figuras} figuras)" for i, titulo, paginas, figuras in libros]
    await enviar(update, "Libros cargados:\n" + "\n".join(lineas))


async def cmd_borrar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_admin(update):
        await update.effective_chat.send_message("Solo el administrador puede borrar libros.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.effective_chat.send_message("Uso: /borrar número (el número sale en /libros)")
        return
    titulo = await asyncio.to_thread(biblioteca.borrar, int(context.args[0]))
    await update.effective_chat.send_message(f"Borrado: {titulo}" if titulo else "No hay un libro con ese número.")


def cmd_aspecto(aspecto: str):
    async def manejador(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        await contestar(update, context, " ".join(context.args or []), aspecto)

    return manejador


async def al_pulsar(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.callback_query.answer()
    aspecto = update.callback_query.data.removeprefix("a:")
    pregunta = context.user_data.get("ultima")
    if not pregunta or aspecto not in ASPECTOS:
        await update.effective_chat.send_message("Escríbeme de nuevo la estructura, por favor.")
        return
    await contestar(update, context, pregunta, aspecto)


async def al_escribir(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await contestar(update, context, update.message.text, None)


async def al_recibir_archivo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not es_admin(update):
        await update.effective_chat.send_message("Solo el administrador puede cargar libros. Escríbeme tu pregunta.")
        return
    documento = update.message.document
    if documento.file_size and documento.file_size > MAX_TELEGRAM:
        await update.effective_chat.send_message(
            "Ese archivo pasa de 20 MB y Telegram no me deja descargarlo. Ábrelo en la app Finlay, "
            "usa «Exportar texto» y envíame el .txt, que pesa muy poco."
        )
        return
    await update.effective_chat.send_message(f"Leyendo «{documento.file_name}»…")
    archivo = await documento.get_file()
    datos = bytes(await archivo.download_as_bytearray())
    await update.effective_chat.send_message(await asyncio.to_thread(cargar, documento.file_name or "libro", datos))


async def al_fallar(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    registro.error("Error atendiendo un mensaje", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("Ocurrió un error. Inténtalo de nuevo en un momento.")


# ---------- carga de libros ----------


def cargar(nombre: str, datos: bytes) -> str:
    """Guarda en la biblioteca los libros de un archivo y devuelve un mensaje con el resultado."""
    try:
        libros = leer_archivo(nombre, datos)
    except ValueError as error:
        return str(error)
    except Exception:
        registro.exception("No se pudo leer %s", nombre)
        return f"No pude leer «{nombre}». Puede estar dañado o protegido con contraseña."
    lineas = []
    for titulo, etiqueta, paginas, figuras in libros:
        try:
            biblioteca.agregar(titulo, paginas, etiqueta, figuras)
            lineas.append(f"✅ {titulo}: {len(paginas)} páginas, {len(figuras)} figuras")
        except ValueError as error:
            lineas.append(f"⚠️ {error}")
    return "\n".join(lineas) or "Ese archivo no trae libros."


def cargar_al_arrancar() -> None:
    """Carga los libros de la carpeta LIBROS_DIR y de los enlaces LIBROS_URLS que aún no estén."""
    cargados = DATA_DIR / "cargados.txt"
    hechos = set(cargados.read_text(encoding="utf-8").splitlines()) if cargados.exists() and biblioteca.libros() else set()
    pendientes: list[tuple[str, str]] = []
    if LIBROS_DIR.is_dir():
        for ruta in sorted(LIBROS_DIR.iterdir()):
            if ruta.suffix.lower() in (".pdf", ".txt", ".json", ".zip"):
                pendientes.append((f"{ruta.name}:{ruta.stat().st_size}", str(ruta)))
    for enlace in filter(None, (x.strip() for x in os.environ.get("LIBROS_URLS", "").split(","))):
        pendientes.append((enlace, enlace))
    for marca, origen in pendientes:
        if marca in hechos:
            continue
        try:
            if origen.startswith(("http://", "https://")):
                nombre = urllib.parse.unquote(urllib.parse.urlparse(origen).path.rsplit("/", 1)[-1]) or "libro.pdf"
                with urllib.request.urlopen(origen, timeout=300) as respuesta:
                    datos = respuesta.read()
            else:
                nombre, datos = Path(origen).name, Path(origen).read_bytes()
        except OSError as error:
            registro.error("No se pudo abrir %s: %s", origen, error)
            continue
        registro.info("Carga de %s:\n%s", nombre, cargar(nombre, datos))
        hechos.add(marca)
    cargados.write_text("\n".join(sorted(hechos)), encoding="utf-8")


def abrir_puerto(puerto: int) -> None:
    """Página mínima para los servidores que exigen que la aplicación escuche en un puerto."""

    class Pagina(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Finlaymed_bot en marcha")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    threading.Thread(target=HTTPServer(("0.0.0.0", puerto), Pagina).serve_forever, daemon=True).start()


def crear_aplicacion(token: str) -> Application:
    aplicacion = Application.builder().token(token).build()
    aplicacion.add_handler(CommandHandler(["start", "ayuda", "help"], cmd_start))
    aplicacion.add_handler(CommandHandler("id", cmd_id))
    aplicacion.add_handler(CommandHandler("libros", cmd_libros))
    aplicacion.add_handler(CommandHandler("borrar", cmd_borrar))
    for aspecto in ASPECTOS:
        aplicacion.add_handler(CommandHandler(aspecto, cmd_aspecto(aspecto)))
    aplicacion.add_handler(CallbackQueryHandler(al_pulsar, pattern=r"^a:"))
    aplicacion.add_handler(MessageHandler(filters.Document.ALL, al_recibir_archivo))
    aplicacion.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, al_escribir))
    aplicacion.add_error_handler(al_fallar)
    return aplicacion


def main() -> None:
    token = os.environ.get("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Falta la variable TELEGRAM_TOKEN (el token que da @BotFather).")
    if os.environ.get("PORT", "").isdigit():
        abrir_puerto(int(os.environ["PORT"]))
    cargar_al_arrancar()
    registro.info(
        "Libros: %d · IA: %s · administradores: %d",
        len(biblioteca.libros()),
        ia.MODELO if tutor else "desactivada",
        len(ADMINS),
    )
    crear_aplicacion(token).run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
