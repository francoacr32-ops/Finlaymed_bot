"""Biblioteca del bot: guarda el texto de los libros y busca en él con citas.

Cada libro se guarda por páginas, partido en fragmentos. La búsqueda usa BM25
(la misma idea que el buscador de la app Finlay) y cada resultado dice de qué
libro y de qué página sale.
"""

import io
import json
import math
import re
import sqlite3
import threading
import unicodedata
import zipfile
from collections import Counter
from dataclasses import dataclass

VACIAS = set(
    "a al algo ante con como cual cuales cuando de del desde donde dos el ella ellas ellos en "
    "entre era es esa ese eso esta estan este esto fue ha hay la las le les lo los mas me mi "
    "muy no nos o para pero por que se sea ser si sin sobre son su sus tambien te tiene tienen "
    "un una uno unas unos y ya e u dime explica explicame describe describeme habla hablame "
    "sobre quiero saber cuentame".split()
)

# Palabras que acercan la búsqueda a cada aspecto de una estructura.
ASPECTOS = {
    "anatomia": ("Anatomía", "anatomia situacion forma porciones caras bordes configuracion externa"),
    "relaciones": ("Relaciones", "relaciones relaciona anterior posterior lateral medial superior inferior limita"),
    "histologia": ("Histología", "histologia epitelio tejido celulas capas tunica mucosa estructura microscopica"),
    "embriologia": ("Embriología", "embriologia desarrollo origen deriva embrionario semana mesodermo endodermo ectodermo"),
    "fisiologia": ("Fisiología", "fisiologia funcion funciones mecanismo regulacion"),
    "irrigacion": ("Irrigación e inervación", "irrigacion arteria vena drenaje linfatico inervacion nervio"),
}

TAM_FRAGMENTO = 1000


def normalizar(texto: str) -> str:
    """Minúsculas y sin tildes."""
    sin = unicodedata.normalize("NFD", texto.lower())
    return "".join(c for c in sin if unicodedata.category(c) != "Mn")


def raiz(palabra: str) -> str:
    """Une singular y plural: arterias→arteria, pulmones→pulmon, raíces→raiz."""
    if len(palabra) > 4 and palabra.endswith("ces"):
        return palabra[:-3] + "z"
    if len(palabra) > 3 and palabra.endswith("s"):
        palabra = palabra[:-1]
    if len(palabra) > 3 and palabra.endswith("e"):
        palabra = palabra[:-1]
    return palabra


def terminos(texto: str) -> list[str]:
    return [raiz(p) for p in re.findall(r"[a-z0-9]+", normalizar(texto)) if p not in VACIAS and len(p) > 1]


def fragmentar(texto: str, tam: int = TAM_FRAGMENTO) -> list[str]:
    """Parte el texto de una página en trozos de unas pocas oraciones."""
    texto = re.sub(r"[ \t]+", " ", texto).strip()
    if not texto:
        return []
    oraciones = re.split(r"(?<=[.!?:;])\s+|\n{2,}", texto)
    trozos, actual = [], ""
    for o in oraciones:
        o = o.strip()
        while len(o) > tam:  # oración enorme (texto sin puntuación)
            corte = o.rfind(" ", 0, tam)
            corte = corte if corte > 0 else tam
            if actual:
                trozos.append(actual)
                actual = ""
            trozos.append(o[:corte])
            o = o[corte:].strip()
        if not o:
            continue
        if actual and len(actual) + len(o) + 1 > tam:
            trozos.append(actual)
            actual = o
        else:
            actual = f"{actual} {o}".strip()
    if actual:
        trozos.append(actual)
    return trozos


@dataclass
class Resultado:
    titulo: str
    pagina: int
    etiqueta: str
    texto: str
    puntos: float

    @property
    def fuente(self) -> str:
        return f"{self.titulo}, {self.etiqueta} {self.pagina}"


class Biblioteca:
    def __init__(self, ruta_db: str):
        self._candado = threading.RLock()
        self._db = sqlite3.connect(ruta_db, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS libros(
                id INTEGER PRIMARY KEY, titulo TEXT UNIQUE, paginas INTEGER, etiqueta TEXT);
            CREATE TABLE IF NOT EXISTS fragmentos(
                id INTEGER PRIMARY KEY, libro_id INTEGER, pagina INTEGER, texto TEXT);
            """
        )
        self._reindexar()

    # ---------- libros ----------

    def libros(self) -> list[tuple[int, str, int, int]]:
        """(id, título, páginas, fragmentos) de cada libro."""
        with self._candado:
            return self._db.execute(
                "SELECT l.id, l.titulo, l.paginas, COUNT(f.id) FROM libros l "
                "LEFT JOIN fragmentos f ON f.libro_id = l.id GROUP BY l.id ORDER BY l.titulo"
            ).fetchall()

    def tiene(self, titulo: str) -> bool:
        with self._candado:
            return self._db.execute("SELECT 1 FROM libros WHERE titulo = ?", (titulo,)).fetchone() is not None

    def agregar(self, titulo: str, paginas: list[tuple[int, str]], etiqueta: str = "pág.") -> int:
        """Guarda un libro (reemplaza el que tenga el mismo título). Devuelve los fragmentos guardados."""
        filas = [(n, trozo) for n, texto in paginas for trozo in fragmentar(texto)]
        if not filas:
            raise ValueError(
                f"«{titulo}» no tiene texto que se pueda leer. Si es un PDF escaneado (fotos de las páginas), "
                "ábrelo en la app Finlay, usa «Exportar texto» y envíame ese .txt."
            )
        with self._candado:
            self._borrar_por_titulo(titulo)
            cur = self._db.execute(
                "INSERT INTO libros(titulo, paginas, etiqueta) VALUES (?, ?, ?)", (titulo, len(paginas), etiqueta)
            )
            self._db.executemany(
                "INSERT INTO fragmentos(libro_id, pagina, texto) VALUES (?, ?, ?)",
                [(cur.lastrowid, n, t) for n, t in filas],
            )
            self._db.commit()
            self._reindexar()
        return len(filas)

    def borrar(self, libro_id: int) -> str | None:
        with self._candado:
            fila = self._db.execute("SELECT titulo FROM libros WHERE id = ?", (libro_id,)).fetchone()
            if not fila:
                return None
            self._borrar_por_titulo(fila[0])
            self._db.commit()
            self._reindexar()
            return fila[0]

    def _borrar_por_titulo(self, titulo: str) -> None:
        self._db.execute("DELETE FROM fragmentos WHERE libro_id IN (SELECT id FROM libros WHERE titulo = ?)", (titulo,))
        self._db.execute("DELETE FROM libros WHERE titulo = ?", (titulo,))

    # ---------- búsqueda ----------

    def _reindexar(self) -> None:
        filas = self._db.execute(
            "SELECT l.titulo, f.pagina, l.etiqueta, f.texto FROM fragmentos f "
            "JOIN libros l ON l.id = f.libro_id ORDER BY f.id"
        ).fetchall()
        indice: dict[str, list[tuple[int, int]]] = {}
        largos = []
        for i, fila in enumerate(filas):
            cuenta = Counter(terminos(fila[3]))
            largos.append(sum(cuenta.values()))
            for termino, veces in cuenta.items():
                indice.setdefault(termino, []).append((i, veces))
        self._filas, self._indice, self._largos = filas, indice, largos
        self._promedio = (sum(largos) / len(largos)) if largos else 1.0

    def buscar(self, consulta: str, k: int = 6, aspecto: str | None = None) -> list[Resultado]:
        """Los k fragmentos que mejor responden a la consulta, del mejor al peor."""
        with self._candado:
            principales = list(dict.fromkeys(terminos(consulta)))
            if not principales or not self._filas:
                return []
            pesos = {t: 1.0 for t in principales}
            if aspecto in ASPECTOS:
                for t in terminos(ASPECTOS[aspecto][1]):
                    pesos.setdefault(t, 0.35)
            n = len(self._filas)
            puntos: dict[int, float] = {}
            vistos: dict[int, int] = {}
            for termino, peso in pesos.items():
                lista = self._indice.get(termino)
                if not lista:
                    continue
                idf = math.log(1 + (n - len(lista) + 0.5) / (len(lista) + 0.5))
                for i, veces in lista:
                    norma = veces + 1.5 * (0.25 + 0.75 * self._largos[i] / self._promedio)
                    puntos[i] = puntos.get(i, 0.0) + peso * idf * veces * 2.5 / norma
                    if peso == 1.0:
                        vistos[i] = vistos.get(i, 0) + 1
            # El fragmento debe hablar de lo que se preguntó: al menos la mitad de las palabras.
            minimo = max(1, math.ceil(len(principales) / 2))
            buenos = [
                (p * (0.5 + vistos[i] / len(principales)), i) for i, p in puntos.items() if vistos.get(i, 0) >= minimo
            ]
            buenos.sort(reverse=True)
            return [Resultado(*self._filas[i], puntos=p) for p, i in buenos[:k]]


# ---------- lectura de archivos ----------

Libro = tuple[str, str, list[tuple[int, str]]]  # (título, etiqueta, páginas)


def _titulo(nombre: str) -> str:
    base = nombre.replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"\.[A-Za-z0-9]{2,5}$", "", base)
    return re.sub(r"[_\s]+", " ", base).strip() or "Libro"


def _decodificar(datos: bytes) -> str:
    for codificacion in ("utf-8-sig", "cp1252"):
        try:
            return datos.decode(codificacion)
        except UnicodeDecodeError:
            pass
    return datos.decode("utf-8", errors="replace")


def leer_pdf(datos: bytes) -> list[tuple[int, str]]:
    from pypdf import PdfReader

    lector = PdfReader(io.BytesIO(datos))
    return [(i, pagina.extract_text() or "") for i, pagina in enumerate(lector.pages, 1)]


def leer_txt(nombre: str, datos: bytes) -> Libro:
    """Texto normal, o el .txt de «Exportar texto» de Finlay (con marcas === pág. 3 ===)."""
    texto = _decodificar(datos)
    titulo, etiqueta = _titulo(nombre), "parte"
    marcas = list(re.finditer(r"^=== (.+?) (\d+) ===[ \t]*$", texto, flags=re.M))
    if marcas:
        doc = re.search(r"^Documento: (.+)$", texto[: marcas[0].start()], flags=re.M)
        if doc:
            titulo = _titulo(doc.group(1))
        etiqueta = marcas[0].group(1)
        paginas = []
        for m, siguiente in zip(marcas, marcas[1:] + [None]):
            fin = siguiente.start() if siguiente else len(texto)
            paginas.append((int(m.group(2)), texto[m.end() : fin]))
        return titulo, etiqueta, paginas
    trozos = fragmentar(texto, 3000)
    return titulo, etiqueta, list(enumerate(trozos, 1))


def leer_paquete(paquete: dict) -> list[Libro]:
    """Paquete de estudio de Finlay (formato finlay-paquete)."""
    libros = []
    for material in paquete.get("materiales", []):
        paginas = [(int(p.get("n", i)), str(p.get("texto", ""))) for i, p in enumerate(material.get("paginas", []), 1)]
        if paginas:
            libros.append((str(material.get("nombre", "Material")), str(material.get("etiqueta", "pág.")), paginas))
    return libros


def leer_archivo(nombre: str, datos: bytes) -> list[Libro]:
    """Convierte un archivo (PDF, .txt, paquete .json o .zip) en libros listos para guardar."""
    extension = nombre.lower().rsplit(".", 1)[-1] if "." in nombre else ""
    if extension == "pdf":
        return [(_titulo(nombre), "pág.", leer_pdf(datos))]
    if extension == "txt":
        return [leer_txt(nombre, datos)]
    if extension == "json":
        try:
            paquete = json.loads(_decodificar(datos))
        except json.JSONDecodeError as error:
            raise ValueError("Ese .json está dañado y no se puede leer.") from error
        if not isinstance(paquete, dict) or paquete.get("formato") != "finlay-paquete":
            raise ValueError("Ese .json no es un paquete de estudio de Finlay.")
        return leer_paquete(paquete)
    if extension == "zip":
        libros: list[Libro] = []
        with zipfile.ZipFile(io.BytesIO(datos)) as comprimido:
            for dentro in comprimido.namelist():
                if dentro.lower().endswith((".pdf", ".txt", ".json")):
                    try:
                        libros += leer_archivo(dentro, comprimido.read(dentro))
                    except ValueError:
                        pass  # un .json que no es paquete: se ignora
        if not libros:
            raise ValueError("Ese .zip no trae libros (PDF, .txt o paquete de Finlay).")
        return libros
    raise ValueError("Solo leo PDF, .txt, y paquetes de Finlay (.json o .zip).")
