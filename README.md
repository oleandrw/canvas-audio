# Canvas → Lyd

Gjør et Canvas-emne om til lydfiler: hent moduler og sider fra Canvas LMS, rens
HTML-en til lesbar tekst, la eventuelt OpenAI eller Anthropic gjøre teksten egnet
for opplesning, generer én MP3 per Canvas-side med ElevenLabs, og publiser
eventuelt til SoundCloud med én spilleliste per modul.

Fordi ElevenLabs, OpenAI og Anthropic koster penger per kall, er kostnadsestimat,
endringsdeteksjon og budsjettvakt førsteklasses funksjonalitet: applikasjonen
viser hva en jobb anslagsvis vil koste **før** den starter, og genererer aldri
lyd på nytt bare fordi Canvas ble synkronisert.

---

## Innhold

- [Hva applikasjonen gjør](#hva-applikasjonen-gjør)
- [Arkitektur](#arkitektur)
- [Krav](#krav)
- [Installasjon](#installasjon)
- [Environment variables](#environment-variables)
- [Skaffe API-nøkler](#skaffe-api-nøkler)
- [Starte lokalt](#starte-lokalt)
- [Docker](#docker)
- [Tester](#tester)
- [Manuell integrasjonstest](#manuell-integrasjonstest)
- [GitHub og deployment](#github-og-deployment)
- [Kostnader og pris-konfigurasjon](#kostnader-og-pris-konfigurasjon)
- [SoundCloud – begrensninger](#soundcloud--begrensninger)
- [Sikkerhet](#sikkerhet)
- [API-ruter](#api-ruter)
- [Feilsøking](#feilsøking)
- [Viktigste filer](#viktigste-filer)

---

## Hva applikasjonen gjør

Arbeidsflyten i GUI-et:

1. **⚙️ Innstillinger** – legg inn Canvas- og ElevenLabs-nøkler, klikk «Test tilkobling».
2. **🎓 Canvas** – hent emner (eller skriv inn emne-ID), klikk «Synkroniser Canvas».
3. Se strukturen: emne → moduler → sider, med statusmerke per side.
4. Velg sider, eventuell AI-bearbeiding (standard: **Ingen AI**) og ElevenLabs-stemme og -modell.
5. Se estimert tegnmengde og kostnad, klikk **Generer lyd**.
6. **🎧 Generering** – følg framdriften per side; feilede sider kan kjøres på nytt.
7. Spill av, last ned eller regenerer ferdige filer.
8. **☁️ SoundCloud** – last opp og lag én spilleliste per Canvas-modul (valgfritt).

### Filstruktur for lyd

Mappene speiler Canvas, så resultatet er brukbart også utenfor applikasjonen:

```
output/
  kunstig-intelligens-i-praksis/
    01-modul-1-introduksjon/
      01-velkommen.mp3
      02-hva-skal-vi-laere.mp3
      03-om-emnet.mp3
    02-modul-2-tema-1/
      01-introduksjon-til-kunstig-intelligens.mp3
      02-oppsummering.mp3
```

Filnavn saniteres (æøå → ae/oe/aa, ulovlige tegn fjernes, path traversal
blokkeres, Windows-reserverte navn avvises, maks 120 tegn, duplikater får `-2`).
**Originaltittelen ligger alltid i databasen** selv om filnavnet normaliseres.
Vil du heller ha `01 - Introduksjon til KI.mp3`, sett filnavnstil til «title» i
Innstillinger.

### Endringsdeteksjon

Hver side har en avledet status, beregnet fra Canvas-tidsstempel og to hasher
(av originalteksten og av teksten som faktisk ble sendt til tale):

| Status | Betydning |
| --- | --- |
| ✓ Oppdatert | Lyden er laget av nøyaktig dagens tekst |
| ○ Ikke generert | Det finnes ingen lydfil |
| ⚠ Canvas-siden er endret | Innholdet i Canvas er endret etter at lyden ble laget |
| ⚠ Teksten er endret | Den bearbeidede/redigerte teksten er endret |
| ✕ Feilet | Forrige forsøk feilet – meldingen vises på siden |

Utvalgsknapper: **Generer manglende**, **Generer endrede**, **Regenerer valgte**,
**Regenerer alt**. Synkronisering fra Canvas trigger aldri regenerering.

---

## Arkitektur

Én FastAPI-prosess, én container, SQLite. Ingen Redis, Celery, Kubernetes eller
mikrotjenester.

- **HTTP ut** – alle eksterne kall er asynkrone (`httpx.AsyncClient`) bak en felles
  klient med timeouts og retry med eksponentiell backoff. Retry skjer **kun** på
  transiente feil (timeout, nettverksfeil, 429, 5xx); 401/403/404 feiler umiddelbart.
  OpenAI og Anthropic går via sine offisielle SDK-er.
- **Database** – synkron SQLAlchemy 2.0. Ruter som bare snakker med databasen er
  `def` (FastAPI kjører dem i trådpool); ruter som kaller eksterne API-er er
  `async def`. Bakgrunnsjobben pakker databasearbeid i `asyncio.to_thread`.
- **Jobbkø** – én in-process `asyncio.Queue` med én worker startet i `lifespan`.
  All jobbstatus ligger i SQLite, så framdrift overlever en omstart og GUI-et
  kan polle. En feil på én side stopper aldri køen.
- **Frontend** – Jinja2 + HTMX (vendored, ingen CDN) + litt vanilla JS. Ingen
  byggsteg, ingen Node.

```
app/
  main.py              FastAPI-app, lifespan, middleware, feilhåndtering
  config.py            env → .env-oppslag, maskering, trygg .env-skriver
  database.py          engine, sesjoner, run_db()
  logging_config.py    strukturert logging + redaction av secrets
  security.py          safe_join, Fernet-kryptering, app-passord, PKCE
  pricing.py           laster pricing.toml, kostnadsestimat
  models/core.py       alle SQLAlchemy-modeller
  schemas/             Pydantic request/response
  routes/              ui, health, canvas, pages, generation, elevenlabs,
                       costs, audio, soundcloud, settings
  services/            canvas, elevenlabs, openai, anthropic, soundcloud,
                       text (HTML→tekst), audio (filer), naming, hashing,
                       sync, selection, cost, presenters, prompts, http, errors
  jobs/                queue.py (worker), pipeline.py (per side)
  templates/           base + 6 sider + partials
  static/              css/app.css, js/app.js, js/player.js, vendor/htmx.min.js
```

---

## Krav

- Python 3.11 eller nyere (utviklet og testet på 3.14; CI kjører 3.11–3.13)
- Canvas-konto med API-token
- ElevenLabs-konto med API-nøkkel
- Valgfritt: OpenAI- og/eller Anthropic-nøkkel
- Valgfritt: SoundCloud Artist Pro og godkjent API-applikasjon
- Valgfritt: Docker

---

## Installasjon

```bash
git clone https://github.com/oleandrw/canvas-audio.git
cd canvas-audio
```

### Oppsett av Python

Windows (PowerShell):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Kopier deretter miljøfilen:

```bash
cp .env.example .env
```

---

## Environment variables

Oppslagsrekkefølgen for alle nøkler er **prosessmiljø → `.env` → standardverdi**.
Verdier som kommer fra ekte miljøvariabler markeres som «Fra miljøvariabel» i
GUI-et og kan ikke overskrives derfra – det er med vilje, slik at en server ikke
lar seg omkonfigurere via nettleseren.

| Variabel | Påkrevd | Beskrivelse |
| --- | --- | --- |
| `CANVAS_URL` | ja | Basis-URL, f.eks. `https://skolen.instructure.com` (uten `/api/v1`) |
| `CANVAS_TOKEN` | ja | Personlig Canvas-token |
| `ELEVENLABS_API_KEY` | ja | For lydgenerering |
| `OPENAI_API_KEY` | nei | Bare hvis du velger OpenAI-bearbeiding |
| `ANTHROPIC_API_KEY` | nei | Bare hvis du velger Anthropic-bearbeiding |
| `SOUNDCLOUD_CLIENT_ID` | nei | Valgfri publisering |
| `SOUNDCLOUD_CLIENT_SECRET` | nei | Valgfri publisering |
| `SOUNDCLOUD_REDIRECT_URI` | nei | Må matche det som er registrert hos SoundCloud |
| `APP_PASSWORD` | nei | Settes den, kreves innlogging for hele applikasjonen |
| `SECRET_KEY` | anbefalt | Signerer session-cookien og krypterer lagrede OAuth-tokens |
| `HTTPS_ONLY` | nei | `1` bak HTTPS, slik at session-cookien får `Secure` |
| `DATA_DIR` | nei | Hvor SQLite-filen ligger (standard `./data`) |
| `OUTPUT_DIR` | nei | Hvor lydfilene havner (standard `./output`) |
| `PRICING_FILE` | nei | Alternativ prisfil |
| `DATABASE_URL` | nei | Overstyr hele databasetilkoblingen |
| `LOG_LEVEL` | nei | `DEBUG`/`INFO`/`WARNING` (standard `INFO`) |
| `HTTP_TIMEOUT` | nei | Sekunder, standard 60 |

Generer en `SECRET_KEY`:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Er den ikke satt, lages en tilfeldig nøkkel per prosess. Det er greit lokalt, men
da overlever ikke innlogging og SoundCloud-tilkobling en omstart.

### Innstillinger-siden skriver `.env`

Lagrer du nøkler i GUI-et, skrives de til `.env` i prosjektmappa. Skrivingen er
atomisk (temp-fil + `os.replace`) og filen får restriktive rettigheter der OS-et
støtter det. **Dette er ment for lokal eller privat drift.** På en server bør du
bruke vanlige miljøvariabler; de vinner over filen, og GUI-et vil da nekte å
endre dem og si fra om det.

---

## Skaffe API-nøkler

**Canvas** – logg inn → *Konto* → *Innstillinger* → *+ Ny tilgangsnøkkel*. Tokenet
vises bare én gang. Applikasjonen trenger bare lesetilgang. Klarer ikke tokenet å
liste emner, kan du skrive inn emne-ID-en manuelt (tallet i Canvas-URL-en).

**ElevenLabs** – <https://elevenlabs.io> → profilen → *API key*. Stemmer og
modeller hentes dynamisk fra API-et; ingenting er hardkodet, og GUI-et krever at
du velger begge deler eksplisitt før første generering.

**OpenAI** – <https://platform.openai.com/api-keys>. Valgfritt.

**Anthropic** – <https://console.anthropic.com>. Valgfritt.

**SoundCloud** – se [begrensningene](#soundcloud--begrensninger) først.

---

## Starte lokalt

Den enkleste og mest robuste varianten – kjører venv-ens Python direkte, så du
slipper både aktivering og execution policy:

Windows (PowerShell) – to kommandoer, ikke én med `&&`:

```powershell
cd C:\claude\canvas-audio
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8000
```

macOS/Linux:

```bash
cd ~/canvas-audio
./.venv/bin/python -m uvicorn app.main:app --reload --port 8000
```

`cd` først fordi `--reload` overvåker gjeldende katalog; står du i hjemmemappa,
prøver uvicorn å følge med på alt som ligger der.

Har du allerede aktivert det virtuelle miljøet, holder det med:

```bash
uvicorn app.main:app --reload --port 8000
```

> **Windows PowerShell 5.1:** `&&` finnes ikke som kommandoseparator der og gir
> `The token '&&' is not a valid statement separator in this version`. Bruk `;`
> mellom kommandoer, og husk `.\` foran aktiveringsskriptet:
>
> ```powershell
> cd C:\claude\canvas-audio; .\.venv\Scripts\Activate.ps1; uvicorn app.main:app --reload
> ```
>
> Blokkerer execution policy aktiveringen, bruk `python.exe`-varianten over –
> den trenger ingen aktivering.

Åpne <http://localhost:8000>.

Databasen og `output/`-mappa opprettes automatisk ved oppstart.

---

## Docker

```bash
docker build -t canvas-audio .
docker run --rm -p 8000:8000 \
  --env-file .env \
  -v "$(pwd)/data:/data" \
  -v "$(pwd)/output:/output" \
  canvas-audio
```

eller:

```bash
docker compose up -d
```

`./data` (SQLite) og `./output` (MP3-ene) er volumer, så de overlever at
containeren bygges på nytt. `pricing.toml` er montert read-only slik at priser
kan rettes uten å bygge imaget på nytt.

Det finnes to compose-filer:

| Fil | Brukes til | Image |
| --- | --- | --- |
| `docker-compose.yml` | lokal kjøring | bygges fra kildekoden (`build: .`) |
| `docker-compose.prod.yml` | server, via deploy-workflowen | ferdig image fra `ghcr.io/oleandrw/canvas-audio` |

Containeren kjører som bruker `10001`, ikke root. På en Linux-server må derfor
`data/` og `output/` være skrivbare for den brukeren, ellers får appen ikke
laget databasen og stopper ved oppstart:

```bash
sudo chown -R 10001:10001 data output
```

Docker Desktop på Windows og macOS håndhever ikke dette for bind mounts.

---

## Tester

```bash
ruff check app tests
ruff format --check app tests
pytest -q
```

Alle eksterne API-er er mocket – `tests/conftest.py` deaktiverer de ekte
httpx-transportene, så en test som ved et uhell forsøker å nå nettet feiler i
stedet for å bruke penger. Bakgrunnsjobben er også byttet ut i rutetestene.

Testene dekker blant annet: config-oppslag og maskering, filnavn-sanitisering og
path traversal, HTML→tekst, endringsdeteksjon, prisberegning og budsjettgrenser,
Canvas-paginering og feilkoder, ElevenLabs-feil og HTML-vakten, SoundCloud PKCE
og token-rotasjon, alle API-rutene, GUI-rendring, samt en ende-til-ende-kjøring
av hele pipelinen med injiserte fake-klienter.

---

## Manuell integrasjonstest

Automatiske tester gjør aldri betalte kall. Slik verifiserer du mot ekte tjenester:

1. Legg inn `CANVAS_URL` og `CANVAS_TOKEN`. Innstillinger → **Test Canvas** skal
   svare «Tilkoblet som *ditt navn*».
2. Legg inn `ELEVENLABS_API_KEY`. **Test ElevenLabs** skal vise abonnement og
   forbrukte tegn.
3. Canvas → **Hent emner** → velg emne → **Synkroniser Canvas**. Kontroller at
   modulene og sidene stemmer med Canvas, og at rekkefølgen er riktig.
4. Åpne én side. Sjekk at originalteksten er lesbar prosa uten HTML, menyer eller
   løse URL-er.
5. **Hent stemmer og modeller**, velg én av hver, **Lagre valg**.
6. Velg **én** side. Kontroller kostnadsestimatet, og klikk **Generer lyd**.
   Dette koster ekte penger – hold deg til én kort side første gang.
7. Spill av filen i GUI-et og sjekk `output/`-mappa.
8. Kjør samme side på nytt: den skal bli **Hoppet over** uten nytt API-kall.
9. Endre teksten i editoren og generer igjen: nå skal den regenereres.
10. Valgfritt: velg OpenAI eller Anthropic og bearbeid én side. Kontroller at
    innholdet er bevart og at ordlyden er blitt mer muntlig.
11. Valgfritt SoundCloud: **Koble til**, **Last opp**, **Synkroniser spillelister**.

---

## GitHub og deployment

Koden ligger i et privat repo: <https://github.com/oleandrw/canvas-audio>.
`.gitignore` dekker `.env`, virtuelle miljøer, `__pycache__/`, `*.db`,
`output/`, IDE- og OS-filer.

```bash
git clone https://github.com/oleandrw/canvas-audio.git
```

Siden repoet er privat, krever kloning at du er innlogget, for eksempel med
`gh auth login` eller Git Credential Manager.

**CI** (`.github/workflows/ci.yml`) kjører på push og pull request: installerer
Python 3.11/3.12/3.13, kjører Ruff (lint + format), importsjekk, hele
testsuiten, og bygger Docker-imaget.

**Deployment** (`.github/workflows/deploy.yml`) er holdt adskilt fra CI og
kjører ved push til `main`:

1. Bygger imaget og pusher det til `ghcr.io/oleandrw/canvas-audio`, merket både
   `latest` og med commit-SHA. Dette skjer alltid.
2. Kobler til serveren over SSH, henter **nøyaktig det imaget som nettopp ble
   bygget** (SHA-merket, ikke `latest`) og starter det med
   `docker-compose.prod.yml`. Dette skjer bare når repo-variabelen
   `DEPLOY_ENABLED` er `true`.

Innloggingen mot registryet på serveren bruker jobbens innebygde token. Tokenet
har bare lesetilgang til pakker, utløper når jobben er ferdig, og serveren logges
ut igjen etterpå, så ingen credential blir liggende igjen. Ingen credentials
ligger i workflow-filen.

### Sette opp en server

1. Installer Docker og Docker Compose på serveren.
2. Lag en mappe, for eksempel `/opt/canvas-audio`, med `data/` og `output/` som
   eies av containerbrukeren:

   ```bash
   sudo mkdir -p /opt/canvas-audio/data /opt/canvas-audio/output
   sudo chown -R 10001:10001 /opt/canvas-audio/data /opt/canvas-audio/output
   ```

3. Kopier `docker-compose.prod.yml` dit, og lag en `.env` ut fra
   `.env.example`. Sett gjerne `SECRET_KEY` og `APP_PASSWORD`, og
   `HTTPS_ONLY=1` hvis serveren står bak HTTPS.
4. Legg inn secrets og slå på deploy i repoet:

   ```bash
   gh secret set DEPLOY_HOST -R oleandrw/canvas-audio
   gh secret set DEPLOY_USER -R oleandrw/canvas-audio
   gh secret set DEPLOY_SSH_KEY -R oleandrw/canvas-audio < ~/.ssh/deploy_key
   gh secret set DEPLOY_PATH -R oleandrw/canvas-audio --body /opt/canvas-audio
   gh variable set DEPLOY_ENABLED -R oleandrw/canvas-audio --body true
   ```

   `DEPLOY_USER` må kunne kjøre `docker` på serveren (for eksempel ved å være i
   `docker`-gruppen).

Endrer du `docker-compose.prod.yml` senere, må du kopiere den til serveren på
nytt. Workflowen henter nye images, men ikke compose-filen.

---

## Kostnader og pris-konfigurasjon

Alle priser ligger i [`pricing.toml`](pricing.toml) – ingenting er hardkodet i
applikasjonslogikken. Når en leverandør endrer priser, retter du filen (eller
peker `PRICING_FILE` et annet sted). Filen inneholder også hvilke AI-modeller
GUI-et tilbyr, og lokal valuta med en manuelt vedlikeholdt kurs.

Før en jobb starter viser GUI-et: antall sider, antall ord, antall tegn, tegn til
ElevenLabs, valgt modell, estimert kostnad per leverandør, sum i USD og i lokal
valuta, samt kostnad per side. Alt er tydelig merket **«Estimert kostnad»** – det
er ikke en garantert pris.

- **Bekreftelsesgrense** (standard $5): jobber over grensen krever en ekstra
  bekreftelse: «Dette vil anslagsvis koste $8.40. Vil du fortsette?»
- **Månedsbudsjett** (standard $25): vises som `$18.73 / $25.00`, med tydelig
  advarsel når en jobb ville sprengt det.
- **Besparelse**: «Ved å generere bare endrede sider sparer du anslagsvis $X.XX
  sammenlignet med å regenerere hele emnet.»
- **Faktisk forbruk** lagres etter hvert kall (tegn for ElevenLabs, faktiske
  input/output-tokens fra `response.usage` for OpenAI og Anthropic) og vises på
  Oversikt fordelt på leverandør, for denne måneden og totalt.

Før hvert betalt kall sjekker applikasjonen om resultatet allerede finnes, om
teksten faktisk har endret seg, og om AI-resultatet kan gjenbrukes (samme
kildetekst, leverandør, modell og promptversjon).

---

## SoundCloud – begrensninger

SoundCloud-laget er bygget på det det offisielle API-et faktisk støtter,
verifisert mot [developers.soundcloud.com](https://developers.soundcloud.com/docs/api/guide)
i september 2026:

- **OAuth 2.1 med PKCE.** `client_credentials` gir bare offentlige ressurser, så
  opplasting og spillelister krever *authorization code*-flyten. Access tokens
  varer omtrent én time, og **refresh tokens er engangsbruk** – applikasjonen
  lagrer det nye tokenet umiddelbart ved hver fornyelse.
- **Opplasting**: `POST /tracks` som multipart med `track[title]`,
  `track[description]`, `track[sharing]` og `track[asset_data]`.
- **Spillelister**: `POST /playlists` med et `playlist`-objekt som inneholder
  `tracks` som `{"id": N}`; oppdatering med `PUT /playlists/{id}` der hele
  sporlisten sendes på nytt.

Praktiske begrensninger du må kjenne til:

1. **Registrering av en API-applikasjon krever Artist Pro-abonnement**, og
   godkjenningen er manuell. Uten dette får du ikke client ID og secret.
2. Det finnes åpne rapporter om at `POST /tracks` tidvis svarer 502/503 for
   enkelte kontoer ([soundcloud/api#451](https://github.com/soundcloud/api/issues/451)).
3. Det er rate limits på client credentials-tokens (50 per 12 timer per
   applikasjon, 30 per time per IP).

Derfor er hele integrasjonen **valgfri**. Uten SoundCloud-credentials viser
GUI-et en tydelig «ikke konfigurert»-tilstand, og Canvas → ElevenLabs fungerer
helt uendret. En lydfil lastes aldri opp to ganger: track-ID og filhash lagres
lokalt, og et uendret spor gjenbrukes. Ett spor kan ligge i flere spillelister.

---

## Sikkerhet

- **Secrets på serveren.** API-nøkler lagres aldri i databasen og sendes aldri i
  klartekst til nettleseren – GUI-et får kun en maskert variant (`sk-…4f2a`).
- **Ingen secrets i logg.** Et redaction-filter på rot-loggeren fjerner både
  konfigurerte nøkkelverdier og alt som ser ut som en credential
  (`Authorization`, `xi-api-key`, `sk-…`, Canvas-tokens), også fra
  tredjepartsbiblioteker.
- **Ingen secrets i Git.** `.env` er gitignored; `.env.example` inneholder ingen
  ekte verdier.
- **Krypterte OAuth-tokens.** SoundCloud-tokens roterer for ofte til å ligge i
  `.env`, så de ligger Fernet-kryptert i databasen med nøkkel utledet fra
  `SECRET_KEY`. Byttes `SECRET_KEY`, blir de ubrukelige og du kobler til på nytt.
- **Sikre filstier.** Alt som skrives eller leses fra `output/` går gjennom
  `safe_join()`, som avviser absolutte stier og alt som peker utenfor mappa.
- **Filnavn-sanitisering** mot ugyldige tegn, path traversal, reserverte
  Windows-navn og ekstremt lange navn.
- **HTML saniteres** før lagring, og templates escaper alt. HTML kan aldri nå
  ElevenLabs: `ensure_plain_text()` kjøres rett før hvert betalte kall.
- **CORS er av.** GUI og API deler origin, så det er ingenting å åpne opp.
- **Valgfri app-auth.** Settes `APP_PASSWORD`, kreves innlogging for alt bortsett
  fra `/api/health` (så container-probes fungerer). Session-cookien er signert,
  `HttpOnly`, `SameSite=Lax`, og `Secure` når `HTTPS_ONLY=1`.

---

## API-ruter

| Metode | Rute | Beskrivelse |
| --- | --- | --- |
| GET | `/api/health` | Helsesjekk (åpen) |
| GET | `/api/canvas/courses` | Emner fra Canvas |
| GET | `/api/canvas/courses/{id}` | Lokalt lagret emnestruktur |
| GET | `/api/canvas/selected` | Valgt emne |
| POST | `/api/canvas/sync` | Synkroniser et emne |
| GET | `/api/pages` | Sider i valgt emne |
| GET | `/api/pages/{id}` | Sidedetaljer |
| PUT | `/api/pages/{id}/text` | Lagre redigert tekst |
| POST | `/api/pages/{id}/reset-text` | Nullstill til original |
| POST | `/api/pages/{id}/clean` | Rens HTML på nytt (gratis) |
| POST | `/api/pages/{id}/process` | AI-bearbeid én side (betalt) |
| POST | `/api/pages/{id}/generate` | Generer lyd for én side |
| POST | `/api/pages/{id}/retry` | Prøv en feilet side på nytt |
| DELETE | `/api/pages/{id}/audio` | Slett lydfilen |
| POST | `/api/generation` | Start en jobb (409 hvis bekreftelse kreves) |
| GET | `/api/generation` | Nylige jobber |
| GET | `/api/generation/active` | Aktiv jobb |
| GET | `/api/generation/{id}` | Jobbstatus med alle sider |
| POST | `/api/generation/{id}/cancel` | Avbryt |
| POST | `/api/generation/{id}/retry-failed` | Kjør bare de feilede sidene |
| POST | `/api/costs/estimate` | Kostnadsestimat |
| GET | `/api/costs/usage` | Faktisk forbruk |
| GET | `/api/elevenlabs/voices` | Tilgjengelige stemmer |
| GET | `/api/elevenlabs/models` | Tilgjengelige modeller |
| POST | `/api/elevenlabs/test` | Test nøkkelen |
| GET | `/api/audio/{page_id}` | Strøm lydfilen (`?download=1` for nedlasting) |
| GET | `/api/soundcloud/status` | Tilkoblingsstatus |
| POST | `/api/soundcloud/connect` | Start OAuth |
| GET | `/api/soundcloud/callback` | OAuth-retur |
| POST | `/api/soundcloud/disconnect` | Koble fra |
| POST | `/api/soundcloud/upload` | Last opp lydfiler |
| POST | `/api/soundcloud/sync-playlists` | Én spilleliste per modul |
| GET | `/api/settings/status` | Maskert nøkkelstatus |
| POST | `/api/settings` | Lagre innstillinger |
| POST | `/api/settings/test/{provider}` | Test én leverandør |

Interaktiv dokumentasjon: <http://localhost:8000/api/docs>.

---

## Feilsøking

**«Canvas avviste tokenet (401)»** – tokenet er utløpt eller feil. Lag et nytt i
Canvas. Sjekk også at `CANVAS_URL` er basis-URL uten `/api/v1`.

**«Canvas nektet tilgang (403)»** – tokenet mangler rettigheter til emnet, eller
emnet er ikke publisert.

**Ingen emner i listen** – noen Canvas-tokens kan ikke liste emner. Skriv inn
emne-ID-en manuelt; det er tallet i Canvas-URL-en (`/courses/41234`).

**«ElevenLabs-kvoten er brukt opp (402)»** – abonnementet er tomt for tegn.
Innstillinger → Test ElevenLabs viser forbruk mot grense.

**«Velg ElevenLabs-stemme og -modell»** – med vilje: det finnes ingen
standardmodell, fordi prisen varierer med en faktor to. Hent stemmer og modeller
på Canvas- eller Innstillinger-siden og lagre valget.

**Siden har ingen tekst** – Canvas-siden er tom, eller den ble synkronisert før
innholdet ble hentet. Kjør «Rens på nytt», eller synkroniser emnet igjen.

**Lydfilen er borte fra disk** – databasen har en rad, men filen er slettet.
Generer på nytt, eller slett lydfilen for å rydde opp i raden.

**Endringer i Innstillinger får ingen effekt** – verdien kommer sannsynligvis fra
en miljøvariabel. GUI-et merker slike felter «Fra miljøvariabel» og sier fra i
lagringsmeldingen.

**SoundCloud 403** – kontoen mangler Artist Pro, eller applikasjonen er ikke
godkjent. Se [begrensningene](#soundcloud--begrensninger).

**Jobben står i kø etter omstart** – jobber som ble avbrutt av en omstart legges
automatisk tilbake i køen ved oppstart, og allerede ferdige sider hoppes over.

---

## Viktigste filer

| Fil | Hvorfor den er viktig |
| --- | --- |
| `app/config.py` | Hvordan nøkler slås opp, maskeres og skrives trygt til `.env` |
| `app/services/text_service.py` | HTML → lesbar tekst, og vakten som hindrer at HTML når TTS |
| `app/services/hashing.py` | Endringsdeteksjonen – bestemmer hva som må betales for |
| `app/services/naming.py` | Filnavn-sanitisering og sti-sikkerhet |
| `app/pricing.py` + `pricing.toml` | All prislogikk og all priskonfigurasjon |
| `app/jobs/pipeline.py` | Per-side-pipelinen: rens → AI → TTS → fil |
| `app/services/prompts.py` | AI-instruksjonen (bump `PROMPT_VERSION` når du endrer den) |
| `app/services/soundcloud_service.py` | Hele SoundCloud-integrasjonen, med API-fakta dokumentert |
| `app/routes/ui.py` | Alle HTML-visningene og HTMX-fragmentene |
| `app/static/css/app.css` | Designsystemet (tokens, komponenter, responsivt) |
| `tests/test_pipeline.py` | Ende-til-ende-beviset for hele kjeden uten betalte kall |
