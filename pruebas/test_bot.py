"""Pruebas sin conexión: python -m unittest discover pruebas"""

import asyncio
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

RINON = (
    "El riñón es un órgano par situado en el espacio retroperitoneal, a ambos lados de la columna vertebral. "
    "Tiene dos caras, dos bordes y dos polos."
)
RELACIONES = (
    "Relaciones del riñón derecho: su cara anterior se relaciona con el hígado, la flexura cólica derecha "
    "y la porción descendente del duodeno. La cara posterior descansa sobre el diafragma y el músculo psoas mayor."
)
EMBRIO = "El riñón definitivo o metanefros deriva del mesodermo intermedio y aparece en la quinta semana del desarrollo."
CORAZON = "El corazón es un órgano muscular hueco situado en el mediastino medio. Las arterias coronarias irrigan el miocardio."


def pdf_minimo(texto: str) -> bytes:
    """Un PDF de una página con una línea de texto."""
    contenido = f"BT /F1 12 Tf 50 700 Td ({texto}) Tj ET".encode("latin-1")
    objetos = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(contenido) + contenido + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    ]
    salida, posiciones = b"%PDF-1.4\n", []
    for i, objeto in enumerate(objetos, 1):
        posiciones.append(len(salida))
        salida += b"%d 0 obj\n" % i + objeto + b"\nendobj\n"
    inicio = len(salida)
    salida += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(b"%010d 00000 n \n" % p for p in posiciones)
    return salida + b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % inicio


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


class Busqueda(unittest.TestCase):
    def setUp(self):
        self.b = bib.Biblioteca(":memory:")
        self.b.agregar("Morfofisiología III", [(10, RINON), (11, RELACIONES), (12, EMBRIO)])
        self.b.agregar("Morfofisiología II", [(40, CORAZON)])

    def test_cita_libro_y_pagina(self):
        r = self.b.buscar("corazon")[0]
        self.assertEqual(r.fuente, "Morfofisiología II, pág. 40")

    def test_el_aspecto_sube_el_fragmento_correcto(self):
        self.assertEqual(self.b.buscar("riñón", aspecto="relaciones")[0].pagina, 11)
        self.assertEqual(self.b.buscar("riñón", aspecto="embriologia")[0].pagina, 12)

    def test_pregunta_completa(self):
        self.assertEqual(self.b.buscar("¿De dónde deriva el riñón definitivo?")[0].pagina, 12)

    def test_lo_que_no_esta_no_aparece(self):
        self.assertEqual(self.b.buscar("páncreas"), [])
        self.assertEqual(self.b.buscar("el de la"), [])

    def test_reemplazar_y_borrar(self):
        self.b.agregar("Morfofisiología II", [(1, "El páncreas es una glándula mixta.")])
        self.assertEqual(self.b.buscar("corazón"), [])
        self.assertEqual(len(self.b.libros()), 2)
        numero = [i for i, titulo, *_ in self.b.libros() if titulo == "Morfofisiología II"][0]
        self.assertEqual(self.b.borrar(numero), "Morfofisiología II")
        self.assertEqual(self.b.buscar("páncreas"), [])

    def test_libro_sin_texto_se_rechaza(self):
        with self.assertRaises(ValueError):
            self.b.agregar("Escaneado", [(1, ""), (2, "   ")])


class Archivos(unittest.TestCase):
    def test_pdf(self):
        ((titulo, etiqueta, paginas),) = bib.leer_archivo("Morfo_I.pdf", pdf_minimo("El femur es un hueso largo"))
        self.assertEqual((titulo, etiqueta, paginas[0][0]), ("Morfo I", "pág.", 1))
        self.assertIn("femur", paginas[0][1])

    def test_texto_exportado_de_finlay(self):
        txt = (
            "FINLAY — TEXTO EXTRAÍDO\nDocumento: Morfofisiología I.pdf\nTipo: libro\nPáginas: 2\n"
            "\n=== pág. 7 ===\nLa célula es la unidad.\n\n=== pág. 8 ===\nEl núcleo contiene el ADN.\n"
        )
        ((titulo, etiqueta, paginas),) = bib.leer_archivo("export.txt", txt.encode())
        self.assertEqual((titulo, etiqueta), ("Morfofisiología I", "pág."))
        self.assertEqual([n for n, _ in paginas], [7, 8])
        self.assertIn("núcleo", paginas[1][1])

    def test_paquete_de_finlay(self):
        paquete = {
            "formato": "finlay-paquete",
            "materiales": [{"nombre": "Conferencia 3", "etiqueta": "diap.", "paginas": [{"n": 1, "texto": "Membrana"}]}],
        }
        self.assertEqual(
            bib.leer_archivo("p.json", json.dumps(paquete).encode()), [("Conferencia 3", "diap.", [(1, "Membrana")])]
        )
        with self.assertRaises(ValueError):
            bib.leer_archivo("otro.json", b'{"a": 1}')
        with self.assertRaises(ValueError):
            bib.leer_archivo("foto.jpg", b"...")


class Chat(unittest.TestCase):
    """El bot atendiendo mensajes, con un Telegram de mentira."""

    def setUp(self):
        for numero, *_ in bot.biblioteca.libros():
            bot.biblioteca.borrar(numero)
        self.enviados = []

    def update(self, usuario=1, texto=None, documento=None):
        async def send_message(texto, reply_markup=None):
            self.enviados.append((texto, reply_markup))

        async def send_action(accion):
            pass

        return SimpleNamespace(
            effective_user=SimpleNamespace(id=usuario),
            effective_chat=SimpleNamespace(send_message=send_message, send_action=send_action),
            message=SimpleNamespace(text=texto, document=documento),
        )

    def documento(self, nombre, datos, tam=None):
        async def download_as_bytearray():
            return bytearray(datos)

        async def get_file():
            return SimpleNamespace(download_as_bytearray=download_as_bytearray)

        return SimpleNamespace(file_name=nombre, file_size=tam or len(datos), get_file=get_file)

    def test_flujo_completo(self):
        contexto = SimpleNamespace(user_data={}, args=[])
        txt = f"=== pág. 10 ===\n{RINON}\n=== pág. 11 ===\n{RELACIONES}\n".encode()

        asyncio.run(bot.al_escribir(self.update(texto="riñón"), contexto))
        self.assertIn("no tengo libros", self.enviados[-1][0])

        asyncio.run(bot.al_recibir_archivo(self.update(usuario=1, documento=self.documento("Morfo III.txt", txt)), contexto))
        self.assertIn("Solo el administrador", self.enviados[-1][0])
        self.assertEqual(bot.biblioteca.libros(), [])

        asyncio.run(bot.al_recibir_archivo(self.update(usuario=7, documento=self.documento("Morfo III.txt", txt)), contexto))
        self.assertIn("✅ Morfo III: 2 páginas", self.enviados[-1][0])

        asyncio.run(bot.al_recibir_archivo(self.update(usuario=7, documento=self.documento("g.pdf", b"", 30_000_000)), contexto))
        self.assertIn("20 MB", self.enviados[-1][0])

        asyncio.run(bot.al_escribir(self.update(texto="¿Dónde está el riñón?"), contexto))
        texto, teclado = self.enviados[-1]
        self.assertIn("📖 Morfo III, pág. 10", texto)
        self.assertEqual(len([b for fila in teclado.inline_keyboard for b in fila]), 6)

        contexto.args = ["riñón"]
        asyncio.run(bot.cmd_aspecto("relaciones")(self.update(), contexto))
        self.assertTrue(self.enviados[-1][0].startswith("📖 Morfo III, pág. 11"))

        asyncio.run(bot.al_escribir(self.update(texto="páncreas"), contexto))
        self.assertEqual(self.enviados[-1][0], bot.NO_ENCONTRE)

    def test_mensaje_largo_se_parte(self):
        asyncio.run(bot.enviar(self.update(), "línea de texto\n" * 700, "teclado"))
        self.assertGreater(len(self.enviados), 1)
        self.assertTrue(all(len(t) <= 4096 for t, _ in self.enviados))
        self.assertEqual([t for _, t in self.enviados if t], ["teclado"])

    def test_los_manejadores_se_registran(self):
        self.assertGreaterEqual(len(bot.crear_aplicacion("123:ABC").handlers[0]), 12)


if __name__ == "__main__":
    unittest.main()
