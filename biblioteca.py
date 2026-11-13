"""Biblioteca del bot: guarda el texto de los libros y busca en él con citas.

Cada libro se guarda por páginas, partido en fragmentos. La búsqueda usa BM25
(la misma idea que el buscador de la app Finlay) y cada resultado dice de qué
libro y de qué página sale.
"""

import gc
import hashlib
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
    "sobre quiero saber cuentame muestra muestrame dame dime menciona mencioname enumera enumerame "
    "ensename ponme pon sacame saca busca buscame necesito acerca cuales todo toda todos todas".split()
)

# Los cuatro apartados de una estructura: (nombre, palabras que lo nombran, palabras que lo acompañan).
ASPECTOS = {
    "anatomia": (
        "Anatomía",
        "anatomia anatomica anatomico situacion situado situada localizado localizada localizacion configuracion "
        "relaciones relaciona relacionan limita irrigacion irrigado irriga inervacion inervado inerva",
        "forma porciones porcion caras cara bordes borde extremos externa macroscopica dimensiones peso pesa mide "
        "anterior posterior lateral medial superior inferior arteria vena nervio hueso articula cavidad "
        "ocupa extiende divide formado compuesto consta",
    ),
    "histologia": (
        "Histología",
        "histologia histologica histologico microscopica microscopico microscopio epitelio epitelial endotelio",
        "tejido celulas celula capas capa tunica mucosa fibras conectivo estroma parenquima membrana basal revestido tapizado",
    ),
    "embriologia": (
        "Embriología",
        "embriologia embriologico embrionario embrionaria embrion desarrollo deriva derivan derivado primordio esbozo "
        "mesodermo endodermo ectodermo mesenquima fetal feto",
        "origen origina semana semanas primitivo primitiva formacion tubo",
    ),
    "fisiologia": (
        "Fisiología",
        "fisiologia fisiologico fisiologica funcion funciones funcional funciona",
        "mecanismo regulacion regula control controla permite bombea contraccion potencial secreta produce "
        "transporta presion flujo gasto actividad responde interviene participa",
    ),
}

TAM_FRAGMENTO = 1000
FIGURAS_POR_PAGINA = 3
PAGINAS_POR_TANDA = 3
PAGINAS_DE_TEMA = 15  # una estructura se busca en un tramo de hasta estas páginas seguidas
TAM_PARTE = 18 * 1024 * 1024  # Telegram solo deja a un bot descargar archivos de hasta 20 MB


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
    encabezado: str = ""
    puntos: float = 0.0
    parrafo: int = -1  # de qué fragmento salió, cuando el texto es una sola oración

    @property
    def fuente(self) -> str:
        return f"{self.titulo}, {self.etiqueta} {self.pagina}"


@dataclass
class Tema:
    """El lugar del libro donde se trata una estructura: un libro y un tramo de páginas seguidas."""

    titulo: str
    etiqueta: str
    paginas: list[int]
    indices: list[int]  # fragmentos de esas páginas, en el orden del libro
    ancla: int = 0  # la página donde empieza a tratarse

    @property
    def fuente(self) -> str:
        if len(self.paginas) == 1:
            return f"{self.titulo}, {self.etiqueta} {self.paginas[0]}"
        return f"{self.titulo}, {self.etiqueta} {self.paginas[0]}–{self.paginas[-1]}"


# ---------- texto de una página ----------

_PIE = re.compile(r"^(Fig(?:ura)?s?\.?)\s*\d+(\.\d+)*", re.I)


def unir(lineas: list[str]) -> str:
    """Une las líneas de un párrafo; repara las palabras cortadas con guion al final del renglón."""
    texto = ""
    for linea in lineas:
        if texto.endswith("-") and linea[:1].islower():
            texto = texto[:-1] + linea
        else:
            texto = f"{texto} {linea}".strip()
    return re.sub(r"\s+", " ", texto)


def _es_encabezado(linea: str, anterior: str | None, siguiente: str | None) -> bool:
    """¿Es el título de un apartado? Renglón corto, sin punto final, tras un párrafo cerrado."""
    if not 3 <= len(linea) <= 70 or len(linea.split()) > 9 or linea[-1] in ".,;" or not linea[0].isupper():
        return False
    if sum(c.isalpha() or c == " " for c in linea) < 0.85 * len(linea):
        return False
    if linea.isupper() and len(linea) > 4:
        return True
    cerrado = anterior is None or anterior[-1] in ".:?!"
    return cerrado and siguiente is not None and siguiente[:1].isupper() and len(siguiente) > 40


def partir_pagina(texto: str, encabezado: str = "") -> tuple[list[tuple[str, str]], list[str], str]:
    """Separa una página en párrafos con su encabezado, y aparte los pies de figura.

    Devuelve (bloques [(encabezado, texto)], pies de figura, último encabezado visto).
    """
    lineas = [linea.strip() for linea in texto.splitlines()]
    lineas = [linea for linea in lineas if linea and not re.fullmatch(r"[\divxlc]{1,5}", linea, flags=re.I)]
    bloques, pies, actual = [], [], []

    def cerrar():
        if actual:
            bloques.append((encabezado, unir(actual)))
            actual.clear()

    i = 0
    while i < len(lineas):
        linea = lineas[i]
        if _PIE.match(linea):
            pie = [linea]
            while pie[-1][-1] != "." and len(pie) < 6 and i + 1 < len(lineas) and not _PIE.match(lineas[i + 1]):
                i += 1
                pie.append(lineas[i])
            pies.append(unir(pie))
        elif _es_encabezado(linea, actual[-1] if actual else None, lineas[i + 1] if i + 1 < len(lineas) else None):
            cerrar()
            encabezado = linea
        else:
            actual.append(linea)
        i += 1
    cerrar()
    return bloques, pies, encabezado


class Biblioteca:
    def __init__(self, ruta_db: str):
        self._candado = threading.RLock()
        self._db = sqlite3.connect(ruta_db, check_same_thread=False)
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS libros(
                id INTEGER PRIMARY KEY, titulo TEXT UNIQUE, paginas INTEGER, etiqueta TEXT);
            CREATE TABLE IF NOT EXISTS crudo(libro_id INTEGER, pagina INTEGER, texto TEXT);
            CREATE TABLE IF NOT EXISTS fragmentos(
                id INTEGER PRIMARY KEY, libro_id INTEGER, pagina INTEGER, texto TEXT, encabezado TEXT);
            CREATE TABLE IF NOT EXISTS pies(libro_id INTEGER, pagina INTEGER, texto TEXT);
            CREATE TABLE IF NOT EXISTS figuras(
                id INTEGER PRIMARY KEY, libro_id INTEGER, pagina INTEGER, datos BLOB);
            CREATE INDEX IF NOT EXISTS figuras_pagina ON figuras(libro_id, pagina);
            CREATE TABLE IF NOT EXISTS archivos(file_id TEXT PRIMARY KEY, nombre TEXT, titulos TEXT);
            CREATE TABLE IF NOT EXISTS copias(titulo TEXT PRIMARY KEY, ids TEXT);
            """
        )
        self._reindexar()

    # ---------- libros ----------

    def libros(self) -> list[tuple[int, str, int, int]]:
        """(id, título, páginas, figuras) de cada libro."""
        with self._candado:
            return self._db.execute(
                "SELECT l.id, l.titulo, l.paginas, (SELECT COUNT(*) FROM figuras g WHERE g.libro_id = l.id) "
                "FROM libros l ORDER BY l.titulo"
            ).fetchall()

    def tiene(self, titulo: str) -> bool:
        with self._candado:
            return self._db.execute("SELECT 1 FROM libros WHERE titulo = ?", (titulo,)).fetchone() is not None

    def agregar(
        self,
        titulo: str,
        paginas: list[tuple[int, str]],
        etiqueta: str = "pág.",
        figuras: list[tuple[int, bytes]] | None = None,
    ) -> int:
        """Guarda un libro (reemplaza el que tenga el mismo título). Devuelve los fragmentos guardados."""
        # Algunos PDF traen caracteres rotos que la base de datos no admite.
        paginas = [(n, texto.encode("utf-8", "ignore").decode("utf-8").replace("\x00", "")) for n, texto in paginas]
        filas, pies, encabezado = [], [], ""
        for n, texto in paginas:
            bloques, de_pagina, encabezado = partir_pagina(texto, encabezado)
            pies += [(n, pie) for pie in de_pagina]
            filas += [(n, trozo, titulo_bloque) for titulo_bloque, parrafo in bloques for trozo in fragmentar(parrafo)]
        if not filas:
            raise ValueError(
                f"«{titulo}» no tiene texto que se pueda leer. Si es un PDF escaneado (fotos de las páginas), "
                "ábrelo en la app Finlay, usa «Exportar texto» y envíame ese .txt."
            )
        with self._candado:
            self._borrar_por_titulo(titulo)
            libro = self._db.execute(
                "INSERT INTO libros(titulo, paginas, etiqueta) VALUES (?, ?, ?)", (titulo, len(paginas), etiqueta)
            ).lastrowid
            self._db.executemany("INSERT INTO crudo VALUES (?, ?, ?)", [(libro, n, t) for n, t in paginas])
            self._db.executemany(
                "INSERT INTO fragmentos(libro_id, pagina, texto, encabezado) VALUES (?, ?, ?, ?)",
                [(libro, n, t, e) for n, t, e in filas],
            )
            self._db.executemany("INSERT INTO pies VALUES (?, ?, ?)", [(libro, n, t) for n, t in pies])
            self._db.executemany(
                "INSERT INTO figuras(libro_id, pagina, datos) VALUES (?, ?, ?)",
                [(libro, n, d) for n, d in figuras or []],
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
            self._olvidar_archivos_de(fila[0])
            self._db.execute("DELETE FROM copias WHERE titulo = ?", (fila[0],))
            self._db.commit()
            self._reindexar()
            return fila[0]

    def _borrar_por_titulo(self, titulo: str) -> None:
        for tabla in ("figuras", "fragmentos", "pies", "crudo"):
            self._db.execute(f"DELETE FROM {tabla} WHERE libro_id IN (SELECT id FROM libros WHERE titulo = ?)", (titulo,))
        self._db.execute("DELETE FROM libros WHERE titulo = ?", (titulo,))

    # ---------- respaldo: archivos de Telegram y copias ya procesadas ----------

    def guardar_archivo(self, file_id: str, nombre: str, titulos: list[str]) -> None:
        """Recuerda de qué archivo de Telegram salieron esos libros."""
        with self._candado:
            for titulo in titulos:
                self._olvidar_archivos_de(titulo)
            self._db.execute("INSERT OR REPLACE INTO archivos VALUES (?, ?, ?)", (file_id, nombre, "\n".join(titulos)))
            self._db.commit()

    def archivos(self) -> list[list]:
        """[file_id, nombre, [títulos]] de cada archivo recibido."""
        with self._candado:
            filas = self._db.execute("SELECT file_id, nombre, titulos FROM archivos ORDER BY rowid").fetchall()
            return [[file_id, nombre, titulos.split("\n")] for file_id, nombre, titulos in filas]

    def _olvidar_archivos_de(self, titulo: str) -> None:
        for file_id, titulos in self._db.execute("SELECT file_id, titulos FROM archivos").fetchall():
            if titulo in titulos.split("\n"):
                self._db.execute("DELETE FROM archivos WHERE file_id = ?", (file_id,))

    def guardar_copia(self, titulo: str, ids: list[str]) -> None:
        with self._candado:
            self._db.execute("INSERT OR REPLACE INTO copias VALUES (?, ?)", (titulo, json.dumps(ids)))
            self._db.commit()

    def copias(self) -> dict[str, list[str]]:
        with self._candado:
            return {t: json.loads(ids) for t, ids in self._db.execute("SELECT titulo, ids FROM copias ORDER BY rowid")}

    def exportar(self, titulo: str, tam_parte: int = TAM_PARTE) -> list[bytes]:
        """El libro ya procesado (texto y figuras), en uno o varios .zip que Telegram deja descargar."""
        with self._candado:
            libro, etiqueta = self._db.execute("SELECT id, etiqueta FROM libros WHERE titulo = ?", (titulo,)).fetchone()
            paginas = self._db.execute("SELECT pagina, texto FROM crudo WHERE libro_id = ? ORDER BY rowid", (libro,)).fetchall()
            figuras = self._db.execute("SELECT pagina, datos FROM figuras WHERE libro_id = ? ORDER BY id", (libro,)).fetchall()
        partes: list[bytes] = []
        memoria, comprimido, peso = None, None, 0

        def abrir():
            nonlocal memoria, comprimido, peso
            memoria = io.BytesIO()
            comprimido, peso = zipfile.ZipFile(memoria, "w"), 0

        def cerrar():
            comprimido.close()
            partes.append(memoria.getvalue())

        abrir()
        ficha = json.dumps({"v": 1, "titulo": titulo, "etiqueta": etiqueta, "paginas": paginas}, ensure_ascii=False)
        comprimido.writestr("libro.json", ficha, zipfile.ZIP_DEFLATED)
        peso = len(ficha) // 2
        for i, (pagina, datos) in enumerate(figuras):
            if peso and peso + len(datos) > tam_parte:
                cerrar()
                abrir()
            comprimido.writestr(f"figuras/{i:05d}_{pagina}.jpg", datos)
            peso += len(datos)
        cerrar()
        return partes

    def importar(self, partes: list[bytes]) -> str:
        """Carga un libro desde las partes que hizo exportar(). Devuelve su título."""
        ficha, figuras = None, []
        for parte in partes:
            with zipfile.ZipFile(io.BytesIO(parte)) as comprimido:
                for nombre in sorted(comprimido.namelist()):
                    if nombre == "libro.json":
                        ficha = json.loads(comprimido.read(nombre))
                    elif nombre.startswith("figuras/"):
                        figuras.append((int(nombre.rsplit("_", 1)[1].split(".")[0]), comprimido.read(nombre)))
        if not ficha:
            raise ValueError("La copia del libro está incompleta.")
        self.agregar(ficha["titulo"], [(n, t) for n, t in ficha["paginas"]], ficha["etiqueta"], figuras)
        return ficha["titulo"]

    # ---------- búsqueda ----------

    def _reindexar(self) -> None:
        filas = self._db.execute(
            "SELECT l.titulo, f.pagina, l.etiqueta, f.texto, f.encabezado FROM fragmentos f "
            "JOIN libros l ON l.id = f.libro_id ORDER BY f.id"
        ).fetchall()
        indice: dict[str, list[tuple[int, int]]] = {}
        cuentas, largos = [], []
        de_pagina: dict[tuple[str, int], list[int]] = {}
        cuenta_pagina: dict[tuple[str, int], Counter] = {}
        titulos_pagina: dict[tuple[str, int], set[str]] = {}
        for i, (titulo, pagina, _, texto, encabezado) in enumerate(filas):
            cuenta = Counter(terminos(texto))
            cuentas.append(cuenta)
            largos.append(sum(cuenta.values()))
            for termino, veces in cuenta.items():
                indice.setdefault(termino, []).append((i, veces))
            clave = (titulo, pagina)
            de_pagina.setdefault(clave, []).append(i)
            cuenta_pagina.setdefault(clave, Counter()).update(cuenta)
            titulos_pagina.setdefault(clave, set()).update(terminos(encabezado or ""))
        self._filas, self._indice, self._cuentas, self._largos = filas, indice, cuentas, largos
        self._de_pagina, self._cuenta_pagina, self._titulos_pagina = de_pagina, cuenta_pagina, titulos_pagina
        self._promedio = (sum(largos) / len(largos)) if largos else 1.0

    def _idf(self, termino: str) -> float:
        n, con = len(self._filas), len(self._indice.get(termino, ()))
        return math.log(1 + (n - con + 0.5) / (con + 0.5))

    def _resultado(self, i: int, puntos: float = 0.0) -> Resultado:
        return Resultado(*self._filas[i], puntos=puntos)

    def buscar(self, consulta: str, k: int = 6, aspecto: str | None = None) -> list[Resultado]:
        """Los k fragmentos que mejor responden a una pregunta, del mejor al peor."""
        with self._candado:
            principales = list(dict.fromkeys(terminos(consulta)))
            if not principales or not self._filas:
                return []
            pesos = {t: 1.0 for t in principales}
            if aspecto in ASPECTOS:
                for t in _propias(aspecto):
                    pesos.setdefault(t, 0.35)
            puntos: dict[int, float] = {}
            vistos: dict[int, int] = {}
            for termino, peso in pesos.items():
                idf = self._idf(termino)
                for i, veces in self._indice.get(termino, ()):
                    norma = veces + 1.5 * (0.25 + 0.75 * self._largos[i] / self._promedio)
                    puntos[i] = puntos.get(i, 0.0) + peso * idf * veces * 2.5 / norma
                    if peso == 1.0:
                        vistos[i] = vistos.get(i, 0) + 1
            # El fragmento debe hablar de lo que se preguntó: de todas las palabras si son pocas;
            # con una pregunta larga basta la mayoría.
            total = len(principales)
            minimos = [total] if total <= 3 else [total, math.ceil(total * 0.6)]
            for minimo in minimos:
                buenos = sorted(((p, i) for i, p in puntos.items() if vistos.get(i, 0) >= minimo), reverse=True)
                if buenos:
                    return [self._resultado(i, p) for p, i in buenos[:k]]
            # Las palabras están en la misma parte del libro, pero no en el mismo párrafo.
            tema = self.tema(consulta)
            if not tema:
                return []
            del_tema = sorted(((puntos.get(i, 0.0), -i) for i in tema.indices if vistos.get(i)), reverse=True)
            return [self._resultado(-i, p) for p, i in del_tema[:k]]

    def tema(self, consulta: str) -> Tema | None:
        """Busca la parte del libro que trata esa estructura: las páginas seguidas donde más se habla de ella."""
        with self._candado:
            principales = list(dict.fromkeys(terminos(consulta)))
            if not principales or any(t not in self._indice for t in principales):
                return None
            idf = {t: self._idf(t) for t in principales}

            # Un nombre corto («órgano de la visión») debe aparecer junto, no con sus palabras
            # repartidas por la página; de una frase larga basta la mayoría, con su palabra más rara.
            total = len(principales)
            minimo = total if total <= 3 else math.ceil(total * 0.6)
            rara = max(idf, key=idf.get)
            define = _definicion(principales)
            puntos: dict[tuple[str, int], float] = {}
            for clave, cuenta in self._cuenta_pagina.items():
                if not cuenta[rara] or sum(1 for t in principales if cuenta[t]) < minimo:
                    continue
                if 2 <= total <= 3 and not any(self._juntas(i, principales) for i in self._de_pagina[clave]):
                    continue
                valor = sum(idf[t] * min(cuenta[t], 6) for t in principales)
                if set(principales) <= self._titulos_pagina[clave]:
                    valor *= 3  # la página tiene un apartado con ese título
                if define and any(self._cuentas[i][rara] and define.search(normalizar(self._filas[i][3])) for i in self._de_pagina[clave]):
                    valor += 8 * idf[rara]  # aquí se presenta: «El corazón es…»
                puntos[clave] = valor
            if not puntos:
                return None

            def ventana(clave: tuple[str, int]) -> float:
                titulo, p = clave
                return (
                    puntos[clave]
                    + 0.7 * puntos.get((titulo, p + 1), 0)
                    + 0.5 * puntos.get((titulo, p + 2), 0)
                    + 0.3 * puntos.get((titulo, p - 1), 0)
                )

            titulo, ancla = max(puntos, key=ventana)
            inicio = fin = ancla
            hueco = 0
            for p in range(ancla + 1, ancla + PAGINAS_DE_TEMA):
                if (titulo, p) in puntos:
                    fin, hueco = p, 0
                else:
                    hueco += 1
                    if hueco > 2:
                        break
            if (titulo, ancla - 1) in puntos:
                inicio = ancla - 1
            paginas = [p for p in range(inicio, fin + 1) if (titulo, p) in self._de_pagina]
            indices = [i for p in paginas for i in self._de_pagina[(titulo, p)]]
            return Tema(titulo, self._filas[indices[0]][2], paginas, indices, ancla)

    def _juntas(self, i: int, principales: list[str]) -> bool:
        """¿Aparecen todas esas palabras casi seguidas en el fragmento?"""
        if any(not self._cuentas[i][t] for t in principales):
            return False
        palabras = terminos(self._filas[i][3])
        ancho = len(principales) + 2
        return any(set(principales) <= set(palabras[j : j + ancho]) for j in range(len(palabras)))

    def esenciales(
        self, consulta: str, n: int = 3, solo: str | None = None
    ) -> tuple[Tema, dict[str, list[Resultado]]] | None:
        """Lo esencial de un tema: sus oraciones clave, repartidas en anatomía, histología, embriología y fisiología.

        Todo sale del libro donde se trata el tema. Cada oración nombra el tema (así se entiende sola)
        y va al apartado cuyas palabras usa. Devuelve hasta n por apartado, en el orden del libro.
        """
        with self._candado:
            tema = self.tema(consulta)
            if not tema:
                return None
            principales = list(dict.fromkeys(terminos(consulta)))
            rara = max(principales, key=self._idf)
            define = _definicion(principales)
            cerca = set(tema.paginas)
            candidatas: dict[str, list] = {a: [] for a in ASPECTOS}
            vistas: set[str] = set()
            orden = 0
            for i, (titulo, pagina, etiqueta, texto, encabezado) in enumerate(self._filas):
                if titulo != tema.titulo or not self._cuentas[i][rara]:
                    continue
                del_titulo = set(terminos(encabezado or ""))
                for oracion in re.split(r"(?<=[.!?])\s+(?=[A-ZÁÉÍÓÚÑ])", texto):
                    oracion = oracion.strip()
                    # Completa (empieza con mayúscula y termina en punto) y de un tamaño que se pueda repasar.
                    if not 40 <= len(oracion) <= 380 or not oracion[0].isupper() or oracion[-1] != "." or oracion in vistas:
                        continue
                    palabras = set(terminos(oracion))
                    if not (set(principales) <= palabras if len(principales) <= 2 else rara in palabras):
                        continue
                    vistas.add(oracion)
                    presenta = bool(define and define.search(normalizar(oracion)))
                    mejor, valor = None, 0
                    for aspecto in ASPECTOS:
                        fuertes, debiles = _PALABRAS[aspecto]
                        v = 2 * len(fuertes & palabras) + len(debiles & palabras) + 2 * bool(fuertes & del_titulo)
                        if v > valor:
                            mejor, valor = aspecto, v
                    if mejor is None:
                        if not presenta:
                            continue
                        mejor = "anatomia"
                    if solo and mejor != solo:
                        continue
                    total = valor + 3 * presenta + 2 * (pagina in cerca)
                    candidatas[mejor].append((total, -orden, orden, Resultado(titulo, pagina, etiqueta, oracion, encabezado or "", total, i)))
                    orden += 1
            elegidas = {}
            for aspecto, lista in candidatas.items():
                mejores = sorted(sorted(lista, key=lambda c: c[:2], reverse=True)[:n], key=lambda c: c[2])
                if mejores:
                    elegidas[aspecto] = [c[3] for c in mejores]
            return tema, elegidas

    def parrafos(self, tema: Tema, oraciones: list[Resultado], maximo: int = 10) -> list[Resultado]:
        """Los párrafos completos de donde salieron esas oraciones (y el arranque del tema), para dar contexto."""
        with self._candado:
            indices = list(dict.fromkeys([r.parrafo for r in oraciones if r.parrafo >= 0] + tema.indices[:3]))
            return [self._resultado(i) for i in sorted(indices)[:maximo]]

    def figuras(self, paginas: list[tuple[str, int]], consulta: str = "", maximo: int = 4) -> list[tuple[str, bytes]]:
        """Figuras de esas páginas (título, página), con su pie. Primero las que nombran lo consultado."""
        principales = set(terminos(consulta))
        with self._candado:
            datos_de = {}
            for titulo, pagina in dict.fromkeys(paginas):
                libro = self._db.execute("SELECT id, etiqueta FROM libros WHERE titulo = ?", (titulo,)).fetchone()
                if not libro:
                    continue
                figuras = self._db.execute(
                    "SELECT datos FROM figuras WHERE libro_id = ? AND pagina = ? ORDER BY id", (libro[0], pagina)
                ).fetchall()
                if not figuras:
                    continue
                pies = [t for (t,) in self._db.execute("SELECT texto FROM pies WHERE libro_id = ? AND pagina = ?", (libro[0], pagina))]
                nombra = sum(1 for pie in pies if principales & set(terminos(pie)))
                datos_de[(titulo, pagina)] = (nombra, f"{titulo}, {libro[1]} {pagina}", pies, [d for (d,) in figuras])
        orden = sorted(datos_de.values(), key=lambda x: -x[0])  # estable: a igualdad, el orden pedido
        salida = []
        for _, fuente, pies, figuras in orden:
            for n, datos in enumerate(figuras[:2]):
                if len(pies) == len(figuras):
                    pie = pies[n]
                else:
                    pie = "\n".join(pies) if n == 0 else ""
                salida.append((f"📖 {fuente}\n{pie}".strip()[:1000], datos))
                if len(salida) >= maximo:
                    return salida
        return salida


_PALABRAS = {a: (set(terminos(f)), set(terminos(d)) - set(terminos(f))) for a, (_, f, d) in ASPECTOS.items()}


def _propias(aspecto: str) -> set[str]:
    return _PALABRAS[aspecto][0] | _PALABRAS[aspecto][1]


def _definicion(principales: list[str]) -> re.Pattern | None:
    """Reconoce la oración que presenta el tema: «El corazón es…», «La sangre está formada…»."""
    if not principales:
        return None
    return re.compile(
        rf"\b(?:el|la|los|las)\s+(?:\w+\s+){{0,3}}?{re.escape(principales[-1])}\w{{0,3}}\s+"
        r"(?:es|son|esta|estan|constituye|constituyen|tiene|tienen|presenta|presentan|posee|forma|forman|se\s+\w+)\b"
    )


def aspecto_pedido(consulta: str) -> tuple[str, str] | None:
    """«histología del estómago» → ("histologia", "estomago"): un apartado nombrado en la pregunta."""
    palabras = terminos(consulta)
    hallados = {a for a in ASPECTOS for t in palabras if t in _PALABRAS[a][0]}
    if len(hallados) != 1 or "?" in consulta:
        return None
    aspecto = hallados.pop()
    resto = [p for p in consulta.split() if not set(terminos(p)) & _PALABRAS[aspecto][0]]
    while resto and normalizar(resto[0]) in VACIAS:  # «histología del estómago» → «estómago»
        resto.pop(0)
    return (aspecto, " ".join(resto)) if 0 < len(terminos(" ".join(resto))) <= 4 else None


def es_estructura(consulta: str) -> bool:
    """¿Es solo el nombre de una estructura («órgano de la visión») y no una pregunta concreta?"""
    palabras = terminos(consulta)
    de_aspecto = {t for fuertes, _ in _PALABRAS.values() for t in fuertes}
    return "?" not in consulta and 0 < len(palabras) <= 4 and not de_aspecto & set(palabras)


# ---------- lectura de archivos ----------

Libro = tuple[str, str, list[tuple[int, str]], list[tuple[int, bytes]]]  # (título, etiqueta, páginas, figuras)


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


def _figuras_de(pagina) -> list[bytes]:
    """Las imágenes de una página del PDF, en JPEG. Se saltan iconos, líneas y adornos."""
    from PIL import Image

    salida = []
    try:
        incrustadas = list(pagina.images)
    except Exception:  # imagen en un formato que pypdf no entiende
        return salida
    for incrustada in incrustadas:
        try:
            imagen = incrustada.image
            if imagen is None:
                continue
            ancho, alto = imagen.size
            if ancho < 220 or alto < 160 or max(ancho, alto) > 6 * min(ancho, alto):
                continue
            if imagen.mode in ("RGBA", "LA", "P"):  # fondo transparente → blanco
                imagen = imagen.convert("RGBA")
                fondo = Image.new("RGB", imagen.size, "white")
                fondo.paste(imagen, mask=imagen.split()[-1])
                imagen = fondo
            imagen = imagen.convert("RGB")
            imagen.thumbnail((1280, 1280))
            memoria = io.BytesIO()
            imagen.save(memoria, "JPEG", quality=78)
            salida.append(memoria.getvalue())
        except Exception:
            continue
        if len(salida) >= FIGURAS_POR_PAGINA:
            break
    return salida


def leer_pdf(datos: bytes) -> tuple[list[tuple[int, str]], list[tuple[int, bytes]]]:
    """Texto y figuras de cada página."""
    from pypdf import PdfReader

    paginas, figuras = [], []
    lector = PdfReader(io.BytesIO(datos))
    total = len(lector.pages)
    for i in range(total):
        # pypdf guarda en memoria cada imagen que descomprime. Con un libro de cientos de figuras
        # eso agota la memoria del servidor, así que el lector se renueva cada pocas páginas.
        if i and i % PAGINAS_POR_TANDA == 0:
            del lector
            gc.collect()
            lector = PdfReader(io.BytesIO(datos))
        pagina = lector.pages[i]
        paginas.append((i + 1, pagina.extract_text() or ""))
        figuras += [(i + 1, d) for d in _figuras_de(pagina)]
    del lector
    gc.collect()
    # Una imagen repetida en muchas páginas es un logotipo o un adorno, no una figura.
    veces = Counter(hashlib.md5(d).digest() for _, d in figuras)
    return paginas, [(n, d) for n, d in figuras if veces[hashlib.md5(d).digest()] <= 3]


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
        return titulo, etiqueta, paginas, []
    trozos = fragmentar(texto, 3000)
    return titulo, etiqueta, list(enumerate(trozos, 1)), []


def leer_paquete(paquete: dict) -> list[Libro]:
    """Paquete de estudio de Finlay (formato finlay-paquete)."""
    libros = []
    for material in paquete.get("materiales", []):
        paginas = [(int(p.get("n", i)), str(p.get("texto", ""))) for i, p in enumerate(material.get("paginas", []), 1)]
        if paginas:
            libros.append((str(material.get("nombre", "Material")), str(material.get("etiqueta", "pág.")), paginas, []))
    return libros


def leer_archivo(nombre: str, datos: bytes) -> list[Libro]:
    """Convierte un archivo (PDF, .txt, paquete .json o .zip) en libros listos para guardar."""
    extension = nombre.lower().rsplit(".", 1)[-1] if "." in nombre else ""
    if extension == "pdf":
        return [(_titulo(nombre), "pág.", *leer_pdf(datos))]
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
