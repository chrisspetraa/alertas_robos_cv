# Alertas de robos en viviendas · Comunitat Valenciana

Bot que vigila los periódicos y buscadores de noticias de la Comunitat Valenciana y te manda a **Telegram** un aviso cada vez que sale una noticia de **robo en vivienda o chalet** (Valencia, Alicante y Castellón). Funciona solo, en la nube, con **GitHub Actions** (gratis) y no necesita que tengas el ordenador encendido.

Ejemplo de aviso:

```
🏠 Robo en vivienda · Alicante · Torrevieja

Detenidos dos hombres por robar en chalets de Torrevieja
La Guardia Civil ha detenido a dos hombres como presuntos autores de...

📰 El Periódico de Aquí · hace 12 min
Leer la noticia
```

## Cómo funciona

1. Cada 10 minutos GitHub ejecuta `bot.py`.
2. El bot lee los feeds RSS de las fuentes de `config.yaml`. Si una fuente no tiene feed conocido, **busca su RSS automáticamente** en la web del medio.
3. Se queda con las noticias que hablan de un delito (robo, asalto, allanamiento...) **y** de una vivienda (chalet, casa, domicilio, urbanización...), descarta falsos positivos (robo de identidad, deportes...) y detecta la provincia y el municipio.
4. Si la misma noticia sale en varios medios, la envía una sola vez, y nunca repite una noticia ya enviada.

Solo se usan el titular, un extracto corto y el enlace a la noticia original.

## Puesta en marcha (unos 15 minutos)

**1. Crear el bot de Telegram.** En Telegram busca `@BotFather`, escribe `/newbot`, elige un nombre y un usuario que acabe en `bot`. Te dará un **token** (`123456:ABC...`). Es como una contraseña: no lo compartas ni lo pegues en ningún fichero.

**2. Sacar tu ID de chat.** Abre tu bot y pulsa *Iniciar* (o escríbele algo). Luego abre en el navegador `https://api.telegram.org/bot<TU_TOKEN>/getUpdates` y busca `"chat":{"id":123456789`. Ese número es tu ID.
- Para que lo reciban **varias personas** (por ejemplo tú y tu padre): cada una debe pulsar *Iniciar* en el bot y pasar su ID; se ponen separados por comas (`111111,222222`).
- Para un **grupo**: crea el grupo, añade el bot, escribe algo en el grupo y repite el `getUpdates`; el ID del grupo empieza por `-`.

**3. Subir el proyecto a GitHub.** Crea una cuenta en github.com, un repositorio nuevo y sube todo el contenido de esta carpeta (*Add file → Upload files*), incluida la carpeta oculta `.github`. Si tu explorador no te deja arrastrar `.github`, usa *Add file → Create new file*, escribe `.github/workflows/alertas.yml` como nombre y pega su contenido.

**4. Guardar los secretos.** En el repositorio: *Settings → Secrets and variables → Actions → New repository secret*. Crea dos:
- `TELEGRAM_BOT_TOKEN` → el token del paso 1
- `TELEGRAM_CHAT_ID` → el ID (o IDs separados por comas) del paso 2

**5. Probar.** Pestaña *Actions* → *Alertas robos CV* → *Run workflow* y elige:
1. `probar-telegram`: debe llegarte un mensaje de prueba.
2. `simulacro`: busca noticias y las muestra en el registro sin enviar nada. Abre la ejecución y mira el **resumen**: te dice qué fuentes funcionan, cuántas noticias leyó de cada una y cuál fue la última.
3. `ejecutar`: ejecución real. Después el bot se lanza solo cada 10 minutos. En la primera ejecución envía un mensaje de bienvenida y como mucho las 5 noticias más recientes.

## Personalizarlo (`config.yaml`)

- **Otros tipos de robo:** en `categorias`, cambia `activa: false` por `true` en `comercios` o `vehiculos` (o crea la tuya).
- **Más fuentes:** añade un bloque en `fuentes` con su `feeds` (dirección RSS) o con `descubrir` (páginas donde buscar el RSS).
- **Más municipios o alias:** amplía la lista `municipios` de su provincia.
- **Menos ruido:** añade palabras a `exclusiones`. **Menos avisos repetidos:** sube `similitud_titulos`.
- **Frecuencia:** cambia el `cron` en `.github/workflows/alertas.yml`.

## Límites que conviene conocer

- **No es "en directo" al segundo.** GitHub programa las ejecuciones cada ~10 minutos, pero a veces las retrasa 10-30 min en horas de mucho tráfico. Además, el aviso sale cuando el medio publica la noticia, no cuando ocurre el robo.
- **Fuentes.** Al montarlo, solo pude comprobar a mano los feeds de *El Periódico de Aquí*. El resto (Levante-EMV, Las Provincias, Información, Mediterráneo, Cadena SER, Plaza, À Punt, Europa Press, La Verdad, Google/Bing Noticias) se prueban al ejecutar y el resumen te dirá cuáles responden. Si alguna falla, pásame el mensaje del registro y la ajusto.
- **Un filtro por palabras no es perfecto.** Puede colar alguna noticia que no es un robo real (p. ej. estadísticas de robos) y no ver una noticia cuyo titular no dice "vivienda/chalet".
- **Repositorio público o privado.** En público, las ejecuciones son gratuitas e ilimitadas y no se expone nada sensible (token e ID van en secretos). En privado, el plan gratuito de GitHub incluye un número limitado de minutos al mes; con ejecuciones cada 10 minutos puede no alcanzar (sube el cron a `*/30`).
- **Pausa por inactividad.** GitHub puede desactivar los programadores de repositorios públicos sin actividad durante 60 días. Si pasa, entra en *Actions* y pulsa *Enable workflow*.
- **Uso personal.** Es una herramienta de lectura de noticias públicas para uso propio, no está vinculada a Verisure. Respeta las condiciones de uso de cada medio y no redistribuyas sus contenidos.

## Desarrollo

```bash
pip install -r requirements.txt pytest
python -m pytest -q                 # pruebas
python bot.py run --dry-run         # simulacro en local (no envía nada)
TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=... python bot.py test-telegram
```
