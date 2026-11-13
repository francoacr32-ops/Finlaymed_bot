"""Pruebas sin conexión: python -m unittest discover pruebas"""

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["DATA_DIR"] = tempfile.mkdtemp()
os.environ["ADMIN_IDS"] = "7"
os.environ.pop("ANTHROPIC_API_KEY", None)

import biblioteca as bib  # noqa: E402
import bot  # noqa: E402

# Un libro de prueba con la forma de los reales: renglones cortados, guiones, pies de figura,
# número de página suelto, un prólogo y una estructura (el corazón) tratada en páginas seguidas.
PROLOGO = """7
Prólogo
Este libro ofrece una visión de la anatomía, la histología y la fisiología de cada órgano
y sistema, como el corazón, para los estudiantes de Medicina."""
CORAZON_1 = """120
CORAZÓN
El corazón es un órgano muscular hueco situado en el mediastino medio. Tiene for-
ma de cono, con una base, un vértice y tres caras. El corazón pesa unos 300 gramos.
Fig. 5.1. Vista anterior del corazón y los grandes
vasos.
Relaciones del corazón
Por delante el corazón se relaciona con el esternón y los cartílagos costales. Por detrás
limita con el esófago y la aorta descendente."""
CORAZON_2 = """121
Estructura histológica
La pared del corazón tiene tres capas: el endocardio, formado por un epitelio simple plano;
el miocardio, de tejido muscular estriado cardíaco, y el epicardio.
Fig. 5.2. Corte histológico del miocardio.
Desarrollo del corazón
El corazón deriva del mesodermo esplácnico. En la tercera semana aparece el tubo cardíaco
primitivo, que luego se pliega."""
CORAZON_3 = """122
Irrigación del corazón
El corazón está irrigado por las arterias coronarias derecha e izquierda. El drenaje venoso
llega al seno coronario.
Funciones del corazón
La función del corazón es bombear la sangre. Su mecanismo de regulación depende del
sistema nervioso autónomo."""
ARTERIAS = """160
Arterias de la cabeza
La arteria carótida externa nace lejos del corazón e irriga la cara. Sus ramos se relacionan
con el nervio hipogloso y son anteriores, posteriores y mediales."""
GENERAL = """40
Hojas germinativas
El embrión trilaminar da origen a todos los órganos: del mesodermo derivan el corazón, los
vasos y la sangre. El desarrollo embrionario sigue en la cuarta semana."""
RINON = "El riñón es un órgano par situado en el espacio retroperitoneal. Tiene dos caras, dos bordes y dos polos."


def biblioteca_de_prueba(b):
    b.agregar("Tomo III", [(7, PROLOGO), (120, CORAZON_1), (121, CORAZON_2), (122, CORAZON_3), (160, ARTERIAS)],
              figuras=[(120, b"fig-120"), (121, b"fig-121"), (160, b"fig-160")])
    b.agregar("Tomo I", [(40, GENERAL), (41, RINON)])


def jpeg(ancho: int, alto: int) -> bytes:
    from PIL import Image

    memoria = io.BytesIO()
    Image.new("RGB", (ancho, alto), (200, 60, 60)).save(memoria, "JPEG")
    return memoria.getvalue()


def pdf_minimo(texto: str, figuras: tuple[tuple[int, int], ...] = ()) -> bytes:
    """Un PDF de una página con una línea de texto y, si se piden, imágenes de esos tamaños."""
    dibujo = "".join(f" q 200 0 0 150 50 {400 - 160 * i} cm /Im{i} Do Q" for i in range(len(figuras)))
    contenido = f"BT /F1 12 Tf 50 700 Td ({texto}) Tj ET{dibujo}".encode("latin-1")
    xobjetos = " ".join(f"/Im{i} {6 + i} 0 R" for i in range(len(figuras)))
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> /XObject << " + xobjetos.encode() + b" >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    for ancho, alto in figuras:
        datos = jpeg(ancho, alto)
        objetos.append(
            b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
            b"/BitsPerComponent 8 /Filter /DCTDecode /Length %d >>\nstream\n" % (ancho, alto, len(datos))
            + datos
            + b"\nendstream"
        )
    salida, posiciones = b"%PDF-1.4\n", []
    for i, objeto in enumerate(objetos, 1):
        posiciones.append(len(salida))
        salida += b"%d 0 obj\n" % i + objeto + b"\nendobj\n"
    inicio = len(salida)
    total = len(objetos) + 1
    salida += b"xref\n0 %d\n0000000000 65535 f \n" % total + b"".join(b"%010d 00000 n \n" % p for p in posiciones)
    return salida + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (total, inicio)


class Texto(unittest.TestCase):
    def test_singular_y_plural_coinciden(self):
        for a, b in [("arterias", "arteria"), ("pulmones", "pulmón"), ("raíces", "raíz"), ("bases", "base")]:
            self.assertEqual(bib.terminos(a), bib.terminos(b))

    def test_fragmentar_respeta_el_tamano_y_no_pierde_texto(self):
        texto = " ".join(f"Oración número {i} sobre el tejido epitelial." for i in range(200))
        trozos = bib.fragmentar(texto)
        self.assertTrue(all(len(t) <= bib.TAM_FRAGMENTO for t in trozos))
        self.assertEqual(" ".join(trozos), texto)
        self.assertEqual("".join(bib.fragmentar("x" * 2500)), "x" * 2500)

    def test_la_pagina_se_limpia(self):
        bloques, pies, ultimo = bib.partir_pagina(CORAZON_1)
        self.assertEqual([e for e, _ in bloques], ["CORAZÓN", "Relaciones del corazón"])
        cuerpo = bloques[0][1]
        self.assertTrue(cuerpo.startswith("El corazón es un órgano"))  # sin el «120» suelto
        self.assertIn("Tiene forma de cono", cuerpo)  # «for-/ma» reparado y renglones unidos
        self.assertNotIn("Fig.", cuerpo)
        self.assertEqual(pies, ["Fig. 5.1. Vista anterior del corazón y los grandes vasos."])
        self.assertEqual(ultimo, "Relaciones del corazón")


class Busqueda(unittest.TestCase):
    def setUp(self):
        self.b = bib.Biblioteca(":memory:")
        biblioteca_de_prueba(self.b)

    def test_el_tema_empieza_donde_el_libro_lo_presenta(self):
        tema = self.b.tema("corazón")
        self.assertEqual((tema.titulo, tema.ancla, tema.paginas), ("Tomo III", 120, [120, 121, 122]))

    def test_lo_esencial_en_cuatro_apartados_y_de_un_solo_libro(self):
        tema, por_aspecto = self.b.esenciales("corazón")
        self.assertEqual(list(por_aspecto), ["anatomia", "histologia", "embriologia", "fisiologia"])
        textos = {a: [r.texto for r in lista] for a, lista in por_aspecto.items()}
        self.assertEqual(textos["anatomia"][0], "El corazón es un órgano muscular hueco situado en el mediastino medio.")
        self.assertIn("Por delante el corazón se relaciona con el esternón y los cartílagos costales.", textos["anatomia"])
        self.assertTrue(textos["histologia"][0].startswith("La pared del corazón tiene tres capas"))
        self.assertEqual(textos["embriologia"], ["El corazón deriva del mesodermo esplácnico."])
        self.assertEqual(textos["fisiologia"], ["La función del corazón es bombear la sangre."])
        todas = [r for lista in por_aspecto.values() for r in lista]
        # Ni el prólogo, ni la carótida, ni el otro tomo; y cada oración nombra el tema.
        self.assertEqual({r.titulo for r in todas}, {"Tomo III"})
        self.assertTrue(all(120 <= r.pagina <= 122 and "corazón" in r.texto for r in todas))
        self.assertTrue(all(len(lista) <= 3 for lista in por_aspecto.values()))

    def test_un_solo_apartado(self):
        _, por_aspecto = self.b.esenciales("corazón", 6, "embriologia")
        self.assertEqual(list(por_aspecto), ["embriologia"])
        self.assertEqual(bib.aspecto_pedido("histología del estómago"), ("histologia", "estómago"))
        self.assertEqual(bib.aspecto_pedido("relaciones del corazón"), ("anatomia", "corazón"))
        self.assertIsNone(bib.aspecto_pedido("corazón"))
        self.assertIsNone(bib.aspecto_pedido("¿Cuál es la función del riñón?"))

    def test_lo_que_no_esta_no_aparece(self):
        # «órgano» está en todas partes; «visión» solo en el prólogo; el ojo no está en estos libros.
        self.assertIsNone(self.b.esenciales("páncreas"))
        self.assertIsNone(self.b.esenciales("órgano de la visión"))
        self.assertEqual(self.b.buscar("páncreas"), [])
        self.assertEqual(self.b.buscar("órgano del olfato"), [])
        self.assertEqual(self.b.buscar("el de la"), [])

    def test_pregunta_concreta(self):
        r = self.b.buscar("¿Con qué se relaciona el corazón por detrás?")[0]
        self.assertEqual(r.fuente, "Tomo III, pág. 120")
        self.assertEqual(self.b.buscar("riñón polos")[0].fuente, "Tomo I, pág. 41")

    def test_distingue_estructura_de_pregunta(self):
        self.assertTrue(bib.es_estructura("órgano de la visión"))
        self.assertTrue(bib.es_estructura("arteria tibial anterior"))
        self.assertFalse(bib.es_estructura("¿Dónde está el riñón?"))
        self.assertTrue(bib.es_estructura("sistema inmune"))
        self.assertFalse(bib.es_estructura("desarrollo del corazón"))

    def test_figuras_con_su_pie_y_primero_las_que_nombran_la_estructura(self):
        figuras = self.b.figuras([("Tomo III", 160), ("Tomo III", 121), ("Tomo III", 120), ("Tomo III", 122)], "corazón")
        self.assertEqual(figuras[0], ("📖 Tomo III, pág. 120\nFig. 5.1. Vista anterior del corazón y los grandes vasos.", b"fig-120"))
        self.assertEqual([d for _, d in figuras], [b"fig-120", b"fig-160", b"fig-121"])

    def test_reemplazar_y_borrar(self):
        self.b.agregar("Tomo I", [(1, "El páncreas es una glándula mixta.")])
        self.assertEqual(self.b.buscar("riñón"), [])
        numero = [i for i, titulo, *_ in self.b.libros() if titulo == "Tomo III"][0]
        self.assertEqual(self.b.borrar(numero), "Tomo III")
        self.assertIsNone(self.b.tema("corazón"))
        for tabla in ("figuras", "pies", "crudo"):
            self.assertEqual(self.b._db.execute(f"SELECT COUNT(*) FROM {tabla} WHERE libro_id = ?", (numero,)).fetchone()[0], 0)

    def test_libro_sin_texto_se_rechaza(self):
        with self.assertRaises(ValueError):
            self.b.agregar("Escaneado", [(1, ""), (2, "   ")])

    def test_copia_procesada_ida_y_vuelta(self):
        partes = self.b.exportar("Tomo III", tam_parte=10)  # tan pequeño que cada figura va en su parte
        self.assertEqual(len(partes), 4)  # la ficha con el texto y tres figuras
        otra = bib.Biblioteca(":memory:")
        self.assertEqual(otra.importar(partes), "Tomo III")
        self.assertEqual(otra.libros()[0][1:], ("Tomo III", 5, 3))
        self.assertEqual(otra.tema("corazón").paginas, [120, 121, 122])
        self.assertEqual(otra.figuras([("Tomo III", 121)])[0][1], b"fig-121")
        with self.assertRaises(ValueError):
            otra.importar(partes[1:])


class Archivos(unittest.TestCase):
    def test_pdf(self):
        pdf = pdf_minimo("El femur es un hueso largo", figuras=((640, 480), (40, 40), (900, 20)))
        ((titulo, etiqueta, paginas, figuras),) = bib.leer_archivo("Morfo_I.pdf", pdf)
        self.assertEqual((titulo, etiqueta, paginas[0][0]), ("Morfo I", "pág.", 1))
        self.assertIn("femur", paginas[0][1])
        # Solo queda la figura de verdad: el icono y la línea se descartan.
        self.assertEqual([n for n, _ in figuras], [1])
        from PIL import Image

        self.assertEqual(Image.open(io.BytesIO(figuras[0][1])).size, (640, 480))

    def test_texto_exportado_de_finlay(self):
        txt = (
            "FINLAY — TEXTO EXTRAÍDO\nDocumento: Morfofisiología I.pdf\nTipo: libro\nPáginas: 2\n"
            "\n=== pág. 7 ===\nLa célula es la unidad.\n\n=== pág. 8 ===\nEl núcleo contiene el ADN.\n"
        )
        ((titulo, etiqueta, paginas, _),) = bib.leer_archivo("export.txt", txt.encode())
        self.assertEqual((titulo, etiqueta), ("Morfofisiología I", "pág."))
        self.assertEqual([n for n, _ in paginas], [7, 8])
        self.assertIn("núcleo", paginas[1][1])

    def test_paquete_de_finlay(self):
        paquete = {
            "formato": "finlay-paquete",
            "materiales": [{"nombre": "Conferencia 3", "etiqueta": "diap.", "paginas": [{"n": 1, "texto": "Membrana"}]}],
        }
        self.assertEqual(
            bib.leer_archivo("p.json", json.dumps(paquete).encode()), [("Conferencia 3", "diap.", [(1, "Membrana")], [])]
        )
        with self.assertRaises(ValueError):
            bib.leer_archivo("otro.json", b'{"a": 1}')
        with self.assertRaises(ValueError):
            bib.leer_archivo("foto.jpg", b"...")


def exportado(*paginas):
    """Un .txt como el de «Exportar texto» de Finlay."""
    return "".join(f"=== pág. {p.split()[0]} ===\n{p}\n" for p in paginas).encode()


class Chat(unittest.TestCase):
    """El bot atendiendo mensajes, con un Telegram de mentira."""

    def setUp(self):
        for numero, *_ in bot.biblioteca.libros():
            bot.biblioteca.borrar(numero)
        self.enviados, self.fotos, self.bajados = [], [], []
        self.nube = {}  # archivos que «guarda Telegram», por su file_id
        self.chat = SimpleNamespace(pinned_message=None, ultimo=None)

    def bot_falso(self):
        async def send_message(admin, texto, **_):
            self.chat.ultimo = SimpleNamespace(message_id=1, text=texto)
            return self.chat.ultimo

        async def pin_chat_message(admin, message_id, **_):
            self.chat.pinned_message = self.chat.ultimo

        async def unpin_all_chat_messages(admin):
            self.chat.pinned_message = None

        async def get_chat(admin):
            return self.chat

        async def get_file(file_id):
            async def download_as_bytearray():
                self.bajados.append(file_id)
                return bytearray(self.nube[file_id])

            return SimpleNamespace(download_as_bytearray=download_as_bytearray)

        async def send_document(chat, documento, **_):
            file_id = f"copia-{len(self.nube)}"
            self.nube[file_id] = documento.input_file_content
            return SimpleNamespace(document=SimpleNamespace(file_id=file_id))

        return SimpleNamespace(
            send_message=send_message, pin_chat_message=pin_chat_message, get_chat=get_chat, get_file=get_file,
            unpin_all_chat_messages=unpin_all_chat_messages, send_document=send_document,
        )

    def contexto(self, *args):
        return SimpleNamespace(user_data={}, args=list(args), bot=self.bot_falso())

    def update(self, usuario=1, texto=None, documento=None):
        async def send_message(texto, reply_markup=None):
            self.enviados.append((texto, reply_markup))

        async def send_action(accion):
            pass

        async def send_photo(foto, caption=None):
            self.fotos.append(caption)

        async def send_media_group(grupo):
            self.fotos += [m.caption for m in grupo]

        return SimpleNamespace(
            effective_user=SimpleNamespace(id=usuario),
            effective_chat=SimpleNamespace(
                id=usuario, send_message=send_message, send_action=send_action, send_photo=send_photo, send_media_group=send_media_group
            ),
            message=SimpleNamespace(text=texto, document=documento),
        )

    def documento(self, nombre, datos, tam=None):
        self.nube[f"id-{nombre}"] = datos

        async def get_file():
            return await self.bot_falso().get_file(f"id-{nombre}")

        return SimpleNamespace(file_name=nombre, file_size=tam or len(datos), get_file=get_file, file_id=f"id-{nombre}")

    def enviar_libro(self, nombre, datos, usuario=7, contexto=None):
        asyncio.run(bot.al_recibir_archivo(self.update(usuario=usuario, documento=self.documento(nombre, datos)), contexto or self.contexto()))

    def test_permisos_y_avisos(self):
        contexto = self.contexto()
        asyncio.run(bot.al_escribir(self.update(texto="riñón"), contexto))
        self.assertIn("no tengo libros", self.enviados[-1][0])
        self.enviar_libro("Tomo I.txt", exportado(GENERAL), usuario=1)
        self.assertIn("Solo el administrador", self.enviados[-1][0])
        self.assertEqual(bot.biblioteca.libros(), [])
        grande = self.documento("g.pdf", b"", 30_000_000)
        asyncio.run(bot.al_recibir_archivo(self.update(usuario=7, documento=grande), contexto))
        self.assertIn("20 MB", self.enviados[-1][0])

    def test_tema_con_lo_esencial_y_sus_fotos(self):
        biblioteca_de_prueba(bot.biblioteca)
        contexto = self.contexto()
        asyncio.run(bot.al_escribir(self.update(texto="corazón"), contexto))
        texto, teclado = self.enviados[-1]
        self.assertTrue(texto.startswith(
            "🔎 CORAZÓN\n📖 Tomo III · se trata desde la pág. 120\n\n"
            "ANATOMÍA\n• El corazón es un órgano muscular hueco situado en el mediastino medio. (pág. 120)"))
        orden = [texto.index(t) for t in ("ANATOMÍA\n", "HISTOLOGÍA\n", "EMBRIOLOGÍA\n• El corazón deriva", "FISIOLOGÍA\n• La función del corazón")]
        self.assertEqual(orden, sorted(orden))
        for ajeno in ("Prólogo", "carótida", "Tomo I ", "trilaminar", "RELACIONES", "IRRIGACIÓN"):
            self.assertNotIn(ajeno, texto)
        self.assertLess(len(texto), 1200)
        self.assertEqual([b.text for fila in teclado.inline_keyboard for b in fila], ["Anatomía", "Histología", "Embriología", "Fisiología"])
        self.assertEqual(len(self.fotos), 2)
        self.assertTrue(self.fotos[0].startswith("📖 Tomo III, pág. 120\nFig. 5.1. Vista anterior del corazón"))

        asyncio.run(bot.cmd_aspecto("embriologia")(self.update(), self.contexto("corazón")))
        self.assertEqual(self.enviados[-1][0], "🔎 EMBRIOLOGÍA · CORAZÓN\n📖 Tomo III · se trata desde la pág. 120\n\n• El corazón deriva del mesodermo esplácnico. (pág. 121)")
        asyncio.run(bot.al_escribir(self.update(texto="histología del corazón"), contexto))
        self.assertTrue(self.enviados[-1][0].startswith("🔎 HISTOLOGÍA · CORAZÓN\n"))

        asyncio.run(bot.al_escribir(self.update(texto="órgano de la visión"), contexto))
        self.assertEqual(self.enviados[-1][0], bot.NO_ENCONTRE)

    def test_pedidos_con_cantidad(self):
        self.assertEqual(bot.pedido("Muéstrame 5 detalles anatómicos del corazón"), ("lista", 5, "anatomia", "muestrame del corazon"))
        self.assertEqual(bot.pedido("tres figuras del tejido epitelial")[:3], ("figuras", 3, None))
        self.assertEqual(bot.pedido("dos funciones del corazón")[:3], ("lista", 2, "fisiologia"))
        self.assertIsNone(bot.pedido("¿Cuáles son las relaciones del estómago?"))

        biblioteca_de_prueba(bot.biblioteca)
        asyncio.run(bot.al_escribir(self.update(texto="Muéstrame 3 detalles anatómicos del corazón"), self.contexto()))
        texto = self.enviados[-1][0]
        self.assertTrue(texto.startswith("📋 MUÉSTRAME 3 DETALLES ANATÓMICOS DEL CORAZÓN\n📖 Tomo III · se trata desde la pág. 120\n\n1. El corazón es un órgano muscular hueco situado en el mediastino medio. (pág. 120)"))
        self.assertIn("\n3. El corazón está irrigado por las arterias coronarias derecha e izquierda. (pág. 122)", texto)
        self.assertNotIn("4. ", texto)

        self.fotos.clear()
        asyncio.run(bot.al_escribir(self.update(texto="1 figura del corazón"), self.contexto()))
        asyncio.run(bot.al_escribir(self.update(texto="5 figuras del corazón"), self.contexto()))
        self.assertEqual(len(self.fotos), 3)  # una del primer pedido y las dos que hay en el segundo
        self.assertIn("solo hay 2", self.enviados[-1][0])
        asyncio.run(bot.al_escribir(self.update(texto="4 detalles del páncreas"), self.contexto()))
        self.assertEqual(self.enviados[-1][0], bot.NO_ENCONTRE)

    def test_respaldo_con_copia_rapida(self):
        self.enviar_libro("Tomo III.txt", exportado(CORAZON_1, CORAZON_2))
        self.assertIn("✅ Tomo III: 2 páginas, 0 figuras", self.enviados[-2][0] + self.enviados[-1][0])
        anclado = json.loads(self.chat.pinned_message.text[len(bot.MARCA):])
        self.assertEqual(anclado["archivos"], [["id-Tomo III.txt", "Tomo III.txt", ["Tomo III"]]])
        self.assertEqual(list(anclado["copias"]), ["Tomo III"])

        # El servidor reinicia y pierde el disco. Al arrancar se recupera de la copia, sin releer el original.
        respaldo = self.chat.pinned_message
        bot.biblioteca.borrar(bot.biblioteca.libros()[0][0])
        self.assertEqual((bot.biblioteca.archivos(), bot.biblioteca.copias()), ([], {}))
        self.bajados.clear()
        asyncio.run(bot.restaurar(self.bot_falso()))
        self.assertEqual(self.bajados, anclado["copias"]["Tomo III"])
        self.assertEqual(bot.biblioteca.tema("corazón").paginas, [120, 121])
        self.assertEqual(bot.biblioteca.archivos(), anclado["archivos"])
        self.assertIs(self.chat.pinned_message, respaldo)  # nada cambió: no se vuelve a anclar
        self.assertFalse(bot.estado["restaurando"])

        # Al borrar el libro, el respaldo se actualiza y ya no lo trae.
        asyncio.run(bot.cmd_borrar(self.update(usuario=7), self.contexto(str(bot.biblioteca.libros()[0][0]))))
        self.assertEqual(json.loads(self.chat.pinned_message.text[len(bot.MARCA):]), {"archivos": [], "copias": {}})

    def test_respaldo_antiguo_se_lee_y_se_le_hace_copia(self):
        self.nube["viejo"] = exportado(CORAZON_1)
        self.chat.pinned_message = SimpleNamespace(text=bot.MARCA + json.dumps([["viejo", "Tomo III.txt"]]))
        asyncio.run(bot.restaurar(self.bot_falso()))
        self.assertEqual([t for _, t, *_ in bot.biblioteca.libros()], ["Tomo III"])
        anclado = json.loads(self.chat.pinned_message.text[len(bot.MARCA):])
        self.assertEqual(anclado["archivos"], [["viejo", "Tomo III.txt", ["Tomo III"]]])
        self.assertEqual(len(anclado["copias"]["Tomo III"]), 1)

    def test_pdf_con_figuras_de_punta_a_punta(self):
        pdf = pdf_minimo("El ojo u organo de la vision esta situado dentro de la orbita.", figuras=((640, 480), (500, 400)))
        self.enviar_libro("Morfo II.pdf", pdf)
        self.assertIn("✅ Morfo II: 1 páginas, 2 figuras", " ".join(t for t, _ in self.enviados))
        asyncio.run(bot.al_escribir(self.update(texto="órgano de la visión"), self.contexto()))
        self.assertEqual(self.fotos, ["📖 Morfo II, pág. 1", "📖 Morfo II, pág. 1"])
        self.assertTrue(self.enviados[-1][0].startswith("🔎 ÓRGANO DE LA VISIÓN\n📖 Morfo II · se trata desde la pág. 1\n\nANATOMÍA\n• El ojo u organo"))

    def test_mensaje_largo_se_parte(self):
        asyncio.run(bot.enviar(self.update(), "línea de texto\n" * 700, "teclado"))
        self.assertGreater(len(self.enviados), 1)
        self.assertTrue(all(len(t) <= 4096 for t, _ in self.enviados))
        self.assertEqual([t for _, t in self.enviados if t], ["teclado"])

    def test_recortar_termina_en_un_punto(self):
        texto = "Primera oración completa. " * 60
        self.assertTrue(bot.recortar(texto).endswith("completa."))
        self.assertLessEqual(len(bot.recortar(texto)), 900)

    def test_token_pegado_con_texto_alrededor(self):
        self.assertEqual(bot.leer_token("API:\n1234567890:AAF-abc_DEF0123456789abcdefghijklmno \n"), "1234567890:AAF-abc_DEF0123456789abcdefghijklmno")
        self.assertIsNone(bot.leer_token("API:"))

    def test_los_manejadores_se_registran(self):
        self.assertGreaterEqual(len(bot.crear_aplicacion("123:ABC").handlers[0]), 12)


if __name__ == "__main__":
    unittest.main()
