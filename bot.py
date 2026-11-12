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
import json
import logging
import os
import re
import threading
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, InputMediaPhoto, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
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
biblioteca = Biblioteca(str(DATA_DIR / "biblioteca2.db"))
tutor = ia.Tutor() if ia.disponible() else None
_usos: dict[tuple[int, datetime.date], int] = {}

# El respaldo es un mensaje anclado en el chat del administrador con los archivos enviados.
# Sirve para volver a cargar los libros solo cuando el servidor borra el disco al reiniciar.
MARCA = "📌 Respaldo de la biblioteca de Finlaymed_bot. No borres ni desancles este mensaje.\n"
estado = {"restaurando": False, "tarea": None}
_un_libro_a_la_vez = threading.Lock()

AYUDA = """Soy el bot de Morfofisiología. Respondo con lo que dicen los libros de Morfofisiología I, II y III, y te digo de qué libro y página sale.

Escríbeme el nombre de una estructura o una pregunta. Por ejemplo:
• riñón
• ¿Cuáles son las relaciones del estómago?
• origen embriológico del corazón

Si escribes solo la estructura (por ejemplo: corazón) voy a la parte del libro donde se trata y te envío sus figuras y, debajo, su anatomía, relaciones, histología, embriología y fisiología.

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


def recortar(texto: str, maximo: int = 900) -> str:
    """Acorta un párrafo largo terminándolo en un punto, no a mitad de una frase."""
    if len(texto) <= maximo:
        return texto
    corto = texto[:maximo]
    punto = corto.rfind(". ")
    return corto[: punto + 1] if punto > maximo // 2 else corto.rsplit(" ", 1)[0] + "…"


def _parrafo(r: Resultado) -> str:
    return f"{r.etiqueta} {r.pagina}\n{recortar(r.texto)}"


def preparar(pregunta: str, aspecto: str | None) -> tuple[str, list[Resultado], list[tuple[str, int]]] | None:
    """Arma la respuesta sin IA. Devuelve (texto, párrafos usados, páginas donde buscar figuras)."""
    if aspecto in ASPECTOS:
        hallado = biblioteca.aspecto(pregunta, aspecto)
        if hallado:
            tema, parrafos = hallado
            texto = f"{ASPECTOS[aspecto][0].upper()} · {pregunta}\n📖 {tema.fuente}\n\n" + "\n\n".join(map(_parrafo, parrafos))
            return texto, parrafos, [(r.titulo, r.pagina) for r in parrafos]
    elif es_estructura(pregunta):
        hallado = biblioteca.resumen(pregunta)
        if hallado:
            # Todo sale de un solo lugar: la parte del libro donde se trata la estructura.
            tema, entrada, secciones = hallado
            partes = [f"🔎 {pregunta.upper()}\n📖 {tema.fuente}", recortar(entrada.texto)]
            partes += [f"{ASPECTOS[a][0].upper()} · {_parrafo(r)}" for a, r in secciones]
            usados = [entrada] + [r for _, r in secciones]
            paginas = [(r.titulo, r.pagina) for r in usados] + [(tema.titulo, p) for p in tema.paginas]
            return "\n\n".join(partes), usados, paginas
        return None
    resultados = biblioteca.buscar(pregunta, 6, aspecto)
    if not resultados:
        return None
    texto = "\n\n".join(f"📖 {r.fuente}\n{recortar(r.texto)}" for r in resultados[:3])
    return texto, resultados, [(r.titulo, r.pagina) for r in resultados[:3]]


async def enviar_figuras(update: Update, figuras: list[tuple[str, bytes]]) -> None:
    """Envía las figuras con su pie: (pie, imagen)."""
    try:
        if len(figuras) == 1:
            await update.effective_chat.send_photo(figuras[0][1], caption=figuras[0][0])
        elif figuras:
            await update.effective_chat.send_media_group([InputMediaPhoto(datos, caption=pie) for pie, datos in figuras])
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
        await update.effective_chat.send_message(
            "Estoy cargando los libros después de un reinicio. Escríbeme de nuevo en unos minutos."
            if estado["restaurando"]
            else "Todavía no tengo libros cargados. El administrador debe enviármelos."
        )
        return
    context.user_data["ultima"] = pregunta
    await update.effective_chat.send_action(ChatAction.TYPING)
    preparado = await asyncio.to_thread(preparar, pregunta, aspecto)
    if not preparado:
        await update.effective_chat.send_message(NO_ENCONTRE)
        return
    texto, parrafos, paginas = preparado
    respuesta = None
    if tutor and puede_usar_ia(update.effective_user.id):
        respuesta = await tutor.responder(pregunta, aspecto, parrafos)
    await enviar_figuras(update, await asyncio.to_thread(biblioteca.figuras, paginas, pregunta))
    await enviar(update, respuesta or texto, botones())


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
    if titulo:
        await respaldar(context.bot)
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
    nombre = documento.file_name or "libro"
    mensaje, titulos = await asyncio.to_thread(cargar, nombre, datos)
    await update.effective_chat.send_message(mensaje)
    if titulos:
        biblioteca.guardar_archivo(documento.file_id, nombre, titulos)
        await copiar(context.bot, update.effective_chat.id, titulos)
        await respaldar(context.bot)


async def al_fallar(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    registro.error("Error atendiendo un mensaje", exc_info=context.error)
    if isinstance(update, Update) and update.effective_chat:
        await update.effective_chat.send_message("Ocurrió un error. Inténtalo de nuevo en un momento.")


# ---------- carga de libros ----------


async def copiar(bot, chat: int, titulos: list[str]) -> None:
    """Guarda en el chat una copia ya procesada de cada libro.

    Leer un PDF tarda minutos en un servidor pequeño; recuperar la copia, segundos.
    """
    for titulo in titulos:
        try:
            partes = await asyncio.to_thread(biblioteca.exportar, titulo)
            ids = []
            for n, parte in enumerate(partes, 1):
                mensaje = await bot.send_document(
                    chat,
                    InputFile(parte, filename=f"copia-{n}-de-{len(partes)}.finlay"),
                    caption=f"📦 Copia de «{titulo}» ({n}/{len(partes)}). No la borres: con ella arranco rápido tras un reinicio.",
                    disable_notification=True,
                    read_timeout=180,
                    write_timeout=180,
                )
                ids.append(mensaje.document.file_id)
            biblioteca.guardar_copia(titulo, ids)
        except Exception:
            registro.exception("No se pudo guardar la copia de %s", titulo)


async def respaldar(bot) -> None:
    """Deja anclada, en el chat de cada administrador, la lista de archivos y copias de la biblioteca."""
    contenido = {"archivos": biblioteca.archivos(), "copias": biblioteca.copias()}
    texto = MARCA + json.dumps(contenido, ensure_ascii=False)
    if len(texto) > 4096:
        registro.warning("El respaldo no cabe en un mensaje: hay demasiados archivos")
        return
    for admin in ADMINS:
        try:
            await bot.unpin_all_chat_messages(admin)
            mensaje = await bot.send_message(admin, texto, disable_notification=True)
            await bot.pin_chat_message(admin, mensaje.message_id, disable_notification=True)
        except TelegramError as error:
            registro.warning("No se pudo dejar el respaldo a %s: %s", admin, error)


async def _bajar(bot, file_id: str) -> bytes:
    archivo = await bot.get_file(file_id)
    return bytes(await archivo.download_as_bytearray())


def _importar(partes: list[bytes]) -> str:
    with _un_libro_a_la_vez:
        return biblioteca.importar(partes)


async def restaurar(bot) -> None:
    """Si la biblioteca está vacía, la recupera del respaldo anclado: primero las copias, que son rápidas."""
    estado["restaurando"] = True
    try:
        for admin in ADMINS:
            try:
                anclado = (await bot.get_chat(admin)).pinned_message
            except TelegramError:
                continue
            if not anclado or not (anclado.text or "").startswith(MARCA):
                continue
            contenido = json.loads(anclado.text[len(MARCA) :])
            if isinstance(contenido, list):  # respaldo de una versión anterior: solo archivos
                contenido = {"archivos": contenido, "copias": {}}
            cambio = False
            for titulo, ids in contenido["copias"].items():
                try:
                    await asyncio.to_thread(_importar, [await _bajar(bot, i) for i in ids])
                    biblioteca.guardar_copia(titulo, ids)
                    registro.info("Recuperado de su copia: %s", titulo)
                except Exception:
                    registro.exception("No sirvió la copia de %s; se leerá el archivo original", titulo)
            for file_id, nombre, *resto in contenido["archivos"]:
                titulos = resto[0] if resto else []
                if titulos and all(biblioteca.tiene(t) for t in titulos):
                    biblioteca.guardar_archivo(file_id, nombre, titulos)
                    continue
                try:
                    datos = await _bajar(bot, file_id)
                except TelegramError as error:
                    registro.error("No se pudo recuperar %s: %s", nombre, error)
                    continue
                mensaje, titulos = await asyncio.to_thread(cargar, nombre, datos)
                registro.info("Restaurado %s:\n%s", nombre, mensaje)
                if titulos:
                    biblioteca.guardar_archivo(file_id, nombre, titulos)
                    await copiar(bot, admin, titulos)
                    cambio = True
            if cambio:
                await respaldar(bot)
            return
    except Exception:
        registro.exception("Falló la restauración de la biblioteca")
    finally:
        estado["restaurando"] = False


async def al_arrancar(aplicacion: Application) -> None:
    if not biblioteca.libros() and ADMINS:
        estado["tarea"] = asyncio.create_task(restaurar(aplicacion.bot))


def _cargar(nombre: str, datos: bytes) -> tuple[str, list[str]]:
    """Guarda en la biblioteca los libros de un archivo. Devuelve el mensaje y los títulos guardados."""
    try:
        libros = leer_archivo(nombre, datos)
    except ValueError as error:
        return str(error), []
    except Exception:
        registro.exception("No se pudo leer %s", nombre)
        return f"No pude leer «{nombre}». Puede estar dañado o protegido con contraseña.", []
    lineas, titulos = [], []
    for titulo, etiqueta, paginas, figuras in libros:
        try:
            biblioteca.agregar(titulo, paginas, etiqueta, figuras)
            titulos.append(titulo)
            lineas.append(f"✅ {titulo}: {len(paginas)} páginas, {len(figuras)} figuras")
        except ValueError as error:
            lineas.append(f"⚠️ {error}")
    return "\n".join(lineas) or "Ese archivo no trae libros.", titulos


def cargar(nombre: str, datos: bytes) -> tuple[str, list[str]]:
    """Como _cargar, pero de uno en uno: leer dos libros a la vez no cabe en la memoria del servidor."""
    with _un_libro_a_la_vez:
        return _cargar(nombre, datos)


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
        registro.info("Carga de %s:\n%s", nombre, cargar(nombre, datos)[0])
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


def leer_token(valor: str) -> str | None:
    """Saca el token aunque se haya pegado con espacios, saltos de línea o texto alrededor."""
    encontrado = re.search(r"\d{6,}:[A-Za-z0-9_-]{30,}", valor)
    return encontrado.group(0) if encontrado else None


def crear_aplicacion(token: str) -> Application:
    aplicacion = Application.builder().token(token).post_init(al_arrancar).build()
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
    token = leer_token(os.environ.get("TELEGRAM_TOKEN", ""))
    if not token:
        raise SystemExit("Falta la variable TELEGRAM_TOKEN, o no contiene un token de @BotFather (123456:ABC...).")
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
