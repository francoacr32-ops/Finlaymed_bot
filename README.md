# Finlaymed_bot 🩺

Bot de Telegram para estudiar **Morfofisiología**. Se guía por los libros de
Morfofisiología I, II y III: le escribes una estructura o una pregunta y responde
con lo que dicen los libros, citando **libro y página**. Si no está en los libros
responde *"No encontré eso en los libros"*.

- **Estructura completa con fotos**: escribes solo la estructura (`órgano de la visión`)
  y envía las figuras que el libro trae en esas páginas y, debajo, el texto por
  apartados: anatomía, relaciones, histología, embriología, fisiología e irrigación.
- Pregunta concreta: `¿cuáles son las relaciones del estómago?`
- Por aspecto: `/anatomia`, `/relaciones`, `/histologia`, `/embriologia`,
  `/fisiologia`, `/irrigacion` seguidos de la estructura (o los botones que salen
  debajo de cada respuesta).
- `/libros` muestra los libros cargados.

Tiene dos modos:

| Modo | Qué hace | Costo |
|---|---|---|
| **Buscador** (por defecto) | Devuelve los párrafos del libro que mejor responden, con su página. | Gratis |
| **Con IA** (si pones `ANTHROPIC_API_KEY`) | Redacta una respuesta ordenada usando solo esos párrafos, con las citas. | La API de Claude es de pago |

## 1. Crear el bot en Telegram

1. En Telegram habla con **@BotFather** → `/newbot` → guarda el **token**.
2. Arranca el bot (paso 2), escríbele `/id` y copia tu número.
3. Pon ese número en la variable `ADMIN_IDS` y reinicia: ya puedes cargar libros.

## 2. Ponerlo en un servidor

El bot no necesita dirección web: se conecta él mismo a Telegram. Sirve cualquier
servidor que ejecute Python 3.11+ o Docker.

```
pip install -r requirements.txt
TELEGRAM_TOKEN=123456:ABC... ADMIN_IDS=111111111 python bot.py
```

Con Docker:

```
docker build -t finlaymed .
docker run -d --restart always -v finlaymed:/datos \
  -e TELEGRAM_TOKEN=123456:ABC... -e ADMIN_IDS=111111111 finlaymed
```

| Variable | Para qué |
|---|---|
| `TELEGRAM_TOKEN` | **Obligatoria.** El token de @BotFather. |
| `ADMIN_IDS` | Ids de quienes pueden cargar y borrar libros, separados por coma. |
| `ANTHROPIC_API_KEY` | Opcional. Activa las respuestas redactadas con IA. |
| `MODELO_IA` | Opcional. Modelo de IA (por defecto `claude-opus-5-5`). |
| `LIMITE_DIARIO` | Opcional. Respuestas con IA por persona y día (por defecto 30); después responde el buscador. |
| `DATA_DIR` | Carpeta donde se guarda la biblioteca (por defecto `datos/`). |
| `LIBROS_DIR` | Carpeta con libros que se cargan al arrancar (por defecto `libros/`). |
| `LIBROS_URLS` | Enlaces directos a libros, separados por coma, que se descargan al arrancar. |
| `PORT` | Si existe, el bot abre una página mínima en ese puerto (algunos servidores lo exigen). |

**El token y las claves van en las variables del servidor, nunca en este repositorio.**

La biblioteca se guarda en `DATA_DIR`. Si el servidor borra el disco en cada
reinicio (pasa en varios planes gratuitos), usa un volumen persistente o
`LIBROS_URLS`, que vuelve a cargar los libros al arrancar.

## 3. Cargar los libros

Los libros **no se suben a GitHub**. Hay tres formas:

1. **Por Telegram** (la más fácil): el administrador le envía al bot el archivo.
   Acepta PDF con texto, `.txt`, y paquetes de Finlay (`.json` / `.zip`).
   Telegram solo deja a los bots descargar archivos de hasta **20 MB**.
2. **Libro grande o escaneado**: ábrelo en la app Finlay → **Exportar texto** →
   envía al bot ese `.txt` (pesa muy poco y conserva las páginas).
3. **En el servidor**: copia los archivos a la carpeta `libros/` o pon sus enlaces
   en `LIBROS_URLS`.

**Las fotos salen solo de los PDF**: al cargar un PDF el bot guarda las figuras de
cada página (descarta iconos y adornos) y las envía junto a la respuesta que cita
esa página. Un `.txt` exportado trae el texto pero no las figuras.

El título del libro sale del nombre del archivo: nómbralos
`Morfofisiología I.pdf`, `Morfofisiología II.pdf`, `Morfofisiología III.pdf`.
Enviar otra vez un libro con el mismo título lo reemplaza. `/borrar número` lo quita.

## Código

```
bot.py          ← comandos y mensajes de Telegram
biblioteca.py   ← guarda los libros (SQLite), lee PDF/txt/paquetes y busca (BM25)
ia.py           ← respuesta redactada con IA (opcional)
pruebas/        ← python -m unittest discover pruebas
```
