# 🩺 Open Medical Radar

**Vigilancia bibliográfica hospitalaria automatizada, crítica y gratuita (Coste 0 €) para médicos especialistas.**

---

## ⚡ ¿Qué es y qué hace en 30 segundos?

**Open Medical Radar** es tu adjunto metodológico automatizado. Cada **lunes a las 08:00 AM** revisa las últimas publicaciones de las revistas líderes de tu especialidad en **PubMed**, descarta el ruido editorial (cartas, erratas, casos aislados y revisiones narrativas sin metaanálisis) y audita los ensayos clínicos y metaanálisis más relevantes mediante un protocolo **PICO** estricto y el **Índice de Impacto Clínico (IIC 0-100)**. Recibes el boletín directamente en tu **correo electrónico** (en formato de tarjetas clínicas) y/o en tu **Telegram**, sin tocar una sola línea de código.

### 📋 Ejemplo visual de tarjeta clínica recibida cada lunes

> 🚨 **[CAMBIO DE PARADIGMA] · IIC: 91/100** *(Metodología: 55/60 · Impacto Hospitalario: 36/40)*  
> **Categoría:** Endocarditis / Bacteriemia  
> **Transición precoz a antibioterapia oral combinada frente a tratamiento endovenoso prolongado en endocarditis izquierda estabilizada (ECA multicéntrico)**  
> *New England Journal of Medicine* · Ensayo Clínico Aleatorizado Multicéntrico · [PMID: 30157394](https://pubmed.ncbi.nlm.nih.gov/30157394/)
>
> * **P (Población):** 400 pacientes adultos con endocarditis infecciosa izquierda por *Streptococcus* spp., *E. faecalis*, *S. aureus* o estafilococos coagulasa-negativos, clínicamente estables tras ≥10 días de tratamiento IV.
> * **I (Intervención):** Paso a biterapia antibiótica oral de alta biodisponibilidad y alta domiciliaria precoz (OPAT oral).
> * **C (Comparador):** Continuación de antibioterapia parenteral intravenosa hospitalaria estándar.
> * **O (Desenlace):** Compuesto primario de mortalidad por cualquier causa, cirugía cardíaca no planificada, embolismo o recaída bacteriémica a los 6 meses.
>
> | Efecto Primario (IC 95%) | RAR / NNT | Significación (p) |
> | :--- | :--- | :--- |
> | Diferencia -3.1% (IC 95%: -7.5 a 1.4) | RAR 3.1% (No inferioridad cumplida) | p < 0.001 |
>
> * **⚠️ Lectura Crítica / Limitaciones:** Diseño abierto tras la aleatorización; población altamente seleccionada con ETE previo que descartó abscesos perivalvulares no drenados (no extrapolable a endocarditis complicada o por microorganismos multirresistentes sin opciones orales).
> * **🏥 Aplicabilidad Hospitalaria Directa:** En pacientes con endocarditis izquierda no complicada, afebriles, con respuesta analítica favorable y ETE sin complicaciones locales tras 10 días IV, permite acortar la estancia hospitalaria de forma segura bajo seguimiento estrecho en Hospitalización a Domicilio / Consultas Externas.

---

## 🚀 Puesta en marcha en 3 pasos (Menos de 5 minutos, Coste 0 €)

No necesitas instalar nada en tu ordenador ni tener conocimientos de informática. Todo funciona en la nube de GitHub de forma gratuita.

### Paso 1: Obtener tu clave gratuita de Google AI Studio (Coste 0 €)
1. Entra en **[Google AI Studio — API Keys](https://aistudio.google.com/app/apikey)** con tu cuenta habitual de Google.
2. Pulsa el botón azul **"Create API key"**.
3. Copia el código generado (empieza por `AIza...`).
   * *Nota de tranquilidad:* La capa gratuita (*Free Tier*) de Google AI Studio es **100 % gratuita (0 €)**, no requiere introducir tarjeta bancaria y ofrece hasta 500 consultas diarias en los modelos *Lite* de respaldo, más que suficiente para analizar toda la literatura médica semanal.

### Paso 2: Elegir por dónde quieres recibir el boletín (Telegram, Email o Ambos)

* **Opción A — Recibirlo en Telegram (Recomendado por rapidez):**
  1. Abre Telegram y busca el usuario oficial verificado **`@BotFather`**.
  2. Escríbele `/newbot`, ponle un nombre a tu asistente (ej. *Mi Radar Médico*) y un alias acabado en `bot` (ej. `radar_interna_antonio_bot`).
  3. Copia el **Token** que te entrega `@BotFather` (ej. `123456789:ABCdef...`) y pulsa en el enlace de tu nuevo bot para darle al botón **Iniciar / Start**.
  4. Para saber tu número de usuario (`TELEGRAM_CHAT_ID`), escribe cualquier mensaje a **`@userinfobot`** en Telegram y te responderá al instante con tu `Id` numérico.

* **Opción B — Recibirlo por Correo Electrónico (Gmail):**
  1. Entra en tu cuenta de Google en **[Contraseñas de aplicaciones de Google](https://myaccount.google.com/apppasswords)** (requiere tener la verificación en 2 pasos activa).
  2. Escribe el nombre `"Radar Medico"` y pulsa **Crear**.
  3. Copia la contraseña de 16 letras que aparece en el recuadro amarillo (esa será tu `SMTP_PASSWORD`).

### Paso 3: Crear tu copia de este repositorio y guardar tus claves
1. En la esquina superior derecha de esta página, pulsa el botón verde **`Use this template`** → **`Create a new repository`**.
2. Ponle el nombre `open-medical-radar` y pulsa **`Create repository`**.
3. En tu nuevo repositorio, entra en la pestaña superior **`Settings`** ⚙️ → en el menú izquierdo despliega **`Secrets and variables`** → **`Actions`**.
4. Pulsa el botón verde **`New repository secret`** para añadir tus claves:

| Nombre del Secreto (`Name`) | Qué debes pegar en `Secret` | ¿Obligatorio? |
| :--- | :--- | :---: |
| `GEMINI_API_KEY` | Tu clave gratuita de Google AI Studio (`AIza...`) | **Sí** |
| `TELEGRAM_BOT_TOKEN` | El token que te dio `@BotFather` | Si usas Telegram |
| `TELEGRAM_CHAT_ID` | Tu número de ID en Telegram | Si usas Telegram |
| `SMTP_SERVER` | `smtp.gmail.com` (si usas Gmail) | Si usas Email |
| `SMTP_PORT` | `587` | Si usas Email |
| `SMTP_USER` | Tu dirección de correo (ej. `tu_correo@gmail.com`) | Si usas Email |
| `SMTP_PASSWORD` | Tu contraseña de aplicación de 16 letras de Google | Si usas Email |
| `EMAIL_TO` | El correo donde quieres recibir el informe semanal | Si usas Email |

> 💡 **Comprobación en 1 clic:** Una vez guardados los secretos, ve a la pestaña **`Actions`** → selecciona **`📡 Radar Bibliográfico Médico Semanal`** a la izquierda → pulsa **`Run workflow`**, escribe `test_connection` y pulsa el botón verde. En 20 segundos verás una tabla en español confirmando que PubMed y Gemini responden correctamente.

---

## 🩺 Cómo configurar tu especialidad en 1 clic (Formulario No-Code)

El sistema incluye 4 plantillas clínicas de alta precisión listas para usar (**Medicina Interna**, **Enfermedades Infecciosas**, **Cirugía General** y **Cardiología**), pero puedes crear tu perfil personalizado sin tocar ningún archivo técnico:

1. Entra en la pestaña superior **`Issues`** de tu repositorio.
2. Pulsa el botón verde **`New issue`**.
3. Selecciona **`🩺 Crear Nuevo Perfil de Vigilancia Bibliográfica`** (`Get started`).
4. Rellena el formulario desplegable con tu nombre, servicio hospitalario, especialidad, canal preferido y (opcionalmente) revistas adicionales o temas que quieras excluir.
5. Pulsa **`Submit new issue`**.

En menos de 30 segundos, el sistema creará automáticamente tu ficha en la carpeta `config/`, cerrará la solicitud y te dejará un mensaje confirmando que tu radar se ejecutará todos los lunes a las 07:00 UTC (08:00 AM en España peninsular).

---

## ❓ Preguntas Frecuentes Clínicas (FAQ)

### 1. ¿Existe algún riesgo con la privacidad de datos de pacientes (RGPD / LOPDGDD)?
**Rotundamente no.** `open-medical-radar` **jamás** se conecta a la historia clínica del hospital ni procesa datos de pacientes. Únicamente descarga metadatos públicos de artículos científicos indexados en la Biblioteca Nacional de Medicina de EE. UU. (PubMed / PubMed Central) y genera resúmenes críticos de literatura publicada.

### 2. ¿Por qué usar este radar en lugar de las alertas básicas por correo de PubMed?
Las alertas clásicas de PubMed envían listados masivos de títulos donde se mezclan cartas al director, casos clínicos aislados, revisiones narrativas de baja calidad, estudios en modelos murinos y erratas editoriales. **Open Medical Radar** aplica un doble filtro:
1. **Cribado metodológico en memoria** que descarta revisiones narrativas, cartas, editoriales y estudios experimentales sin traslación clínica antes de consumir tu cuota.
2. **Auditoría estructurada PICO** que extrae población real, comparador, magnitud cuantitativa del efecto (HR/OR/RR, IC 95%, RAR/NNT, valor *p*), sesgos metodológicos y qué cambia exactamente en tu pase de planta o quirófano.

### 3. ¿Cómo se calcula el Índice de Impacto Clínico (IIC 0-100)?
Cada estudio auditado recibe una puntuación transparente sobre **100 puntos** dividida en dos bloques:
* **Rigor Metodológico (0 a 60 puntos):** Evalúa el diseño (ECA multicéntrico doble ciego o metaanálisis riguroso = 50-60 pts; ECA abierto o cohorte prospectiva ajustada = 35-49 pts; estudio retrospectivo unicéntrico o desenlace subrogado = 0-34 pts), tamaño muestral, potencia estadística y riesgo de sesgos.
* **Impacto Hospitalario Directo (0 a 40 puntos):** Evalúa si el hallazgo modifica las decisiones clínicas reales mañana mismo en urgencias, planta de hospitalización, quirófano o interconsulta (32-40 pts), si confirma el estándar actual en subgrupos (20-31 pts) o si tiene aplicabilidad marginal (0-19 pts).
* Los artículos de revistas troncales que alcanzan un **IIC ≥ 85/100** reciben automáticamente la insignia **`🚨 CAMBIO DE PARADIGMA`**; si un estudio de revista troncal obtiene un IIC < 85, la insignia se degrada honestamente a **`⭐ RELEVANCIA CLÍNICA`** para evitar falsas alarmas.

### 4. ¿Qué ocurre si un lunes hay congestión en los servidores de Google AI Studio?
El motor incorpora una arquitectura de alta disponibilidad diseñada específicamente para la capa gratuita:
* Se ejecuta los **lunes a las 07:00 UTC** (franja valle de tráfico en EE. UU., cuando los servidores de Google están completamente despejados).
* Si el modelo principal (`gemini-3.8-flash`) sufre saturación puntual (`HTTP 503`) o agota su cuota (`HTTP 429`), el sistema aplica esperas inteligentes con *Full Jitter* y conmuta instantáneamente a dos modelos de respaldo independientes (`gemini-3.5-flash-lite` y `gemini-3.1-flash-lite`) con un colchón de **500 peticiones diarias gratuitas**.

---

## 📄 Licencia

Distribuido bajo licencia **MIT**. Libre para uso personal, docente y en servicios clínicos hospitalarios.
