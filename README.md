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
- [Publiser i Canvas](#-publiser-i-canvas)
- [Oppdatering](#️-oppdatering)
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
| ⚠ Teksten fra Canvas er endret | Innholdet i Canvas er endret etter at lyden ble laget — eller appen utleder nå en annen tekst fra samme side (ny uttaleliste, forbedret rensing) |
| ⚠ Teksten er endret | Den bearbeidede/redigerte teksten er endret |
| ✕ Feilet | Forrige forsøk feilet – meldingen vises på siden |

Utvalgsknapper: **Generer manglende**, **Generer endrede**, **Regenerer valgte**,
**Regenerer alt**. Synkronisering fra Canvas trigger aldri regenerering.

Med 1000+ sider i databasen henter `/canvas` bare modulene; sidene i en modul
hentes når du åpner den. Søkefeltet og statusfilteret kjøres på serveren, så de
finner også sider i moduler du ikke har åpnet, og «Velg alle / manglende /
endrede» sender et *modus* til serveren i stedet for tusen avkryssinger.

### Uttaleliste

Talemotoren bommer på forkortelser og egennavn. I **⚙️ Innstillinger → 🗣️ Uttale**
skriver du en liste med «skrevet i Canvas → leses som»: «HiØ» → «Høgskolen i
Østfold», «jf.» → «jamfør», «§» → «paragraf». Oppslagene er ren tekst, ikke
regulære uttrykk, og du velger per oppslag om det skal gjelde hele ord og om
store/små bokstaver skal ha noe å si. Det lengste oppslaget vinner, og en
erstattet tekst blir aldri erstattet en gang til.

Listen brukes **når teksten renses**, ikke som et skjult ekstra steg rett før
tale. Det betyr at teksten du ser på sidevisningen er nøyaktig den teksten som
blir lest opp og som hashes for endringsdeteksjon. Når du lagrer listen, renses
de lagrede sidene på nytt med én gang — uten å kontakte Canvas — og bare sidene
som faktisk endres blir merket som endret.

### Lange sider

ElevenLabs har en grense per forespørsel (40 000 tegn for turbo og flash v2.5,
10 000 for multilingual v2, noen tusen for v3). Sider som er lengre deles
automatisk på setningsgrenser og settes sammen til én lydfil. Hver del sender
nabo-teksten som kontekst (`previous_text`/`next_text`), så tonefallet ikke
hopper i skjøtene. Prisen er den samme — den beregnes per tegn, ikke per
forespørsel. Underveis står det «Del 3 av 5» på siden i generering-tabellen.

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

Du trenger Python 3.11 eller nyere. Last det ned fra
<https://www.python.org/downloads/> og huk av for «Add python.exe to PATH» under
installasjonen. `python --version` viser hvilken versjon du har.

Hent koden:

```bash
git clone https://github.com/oleandrw/canvas-audio.git
```

Repoet er privat, så kloning krever at du er innlogget, for eksempel med
`gh auth login` eller Git Credential Manager.

### Windows: med dobbeltklikk (anbefalt)

1. Dobbeltklikk `install.bat` i prosjektmappa. Den lager det virtuelle miljøet
   (`.venv`), installerer appen og spør om du vil ha et ikon på skrivebordet.
   Første gang tar det noen minutter.
2. Dobbeltklikk `start.bat` (eller ikonet). Appen åpner seg i nettleseren.
3. Legg inn Canvas- og ElevenLabs-nøklene under 🔑 API, og velg stemme og modell
   under ⚙️ Innstillinger.

Hent gjerne prosjektet med `git clone` framfor å laste ned en ZIP: da kan appen
oppdatere seg selv fra [⬆️ Oppdatering](#️-oppdatering).

`install.bat` kan kjøres på nytt når som helst, for eksempel etter en
`git pull`. En `.venv` som finnes fra før gjenbrukes. Den kjører ingen
PowerShell-skript som krever endret execution policy.

For utvikling, med test- og lintverktøyene i tillegg:

```bash
install.bat --dev
```

### Manuelt

Windows (PowerShell). Du trenger ikke aktivere miljøet; kall Python-programmet
i `.venv` direkte:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

Aktivering er valgfritt og bare en snarvei for kortere kommandoer. Bruk
`.venv\Scripts\activate.bat` i cmd. `Activate.ps1` i PowerShell krever at
kjøring av skript er tillatt, noe det ikke er som standard på Windows (se
[Feilsøking](#feilsøking)).

macOS/Linux:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Innstillingene kan legges inn i appen, som lager `.env` selv. Vil du heller
fylle ut filen på forhånd, kopier `.env.example` til `.env`.

---

## 📤 Publiser i Canvas

Menypunktet **Publiser i Canvas** legger lyden rett inn i emnet:

1. Hver lydfil lastes opp til **Filer**, i en mappe per modul, for eksempel
   `Lydfiler/01 Modul 1 – Introduksjon`. Mappenavnet velger du selv.
2. Hver side får en avspiller **øverst**. Resten av siden blir ikke endret.

Du velger utseende på avspilleren (Kort, Stripe, Kompakt, Minimal eller Mørk),
farge, tekst og ikon, og ser resultatet i en forhåndsvisning med ekte lyd før du
publiserer. Canvas tillater ikke egne stilark, så designene bruker bare inline CSS.

Slik oppfører publiseringen seg:

- Bare sider med oppdatert lyd publiseres.
- En lydfil som allerede ligger i Canvas, lastes ikke opp igjen. Er den slettet
  der, eller er lyden generert på nytt, lastes den opp på nytt.
- Publiserer du igjen, byttes avspilleren ut i stedet for at det blir to. Sider som
  allerede er oppdatert, blir ikke rørt.
- Siden hentes fra Canvas rett før den endres, så endringer læreren har gjort
  etter siste synkronisering blir med.
- Canvas tar vare på forrige versjon av hver side (sidehistorikken), og ingen får
  varsel om endringen.
- **Fjern** tar avspilleren bort fra sidene. Lydfilene blir liggende i Filer.
- Avspilleren er merket, så teksten i den aldri blir en del av det som leses opp,
  og siden ikke blir markert som endret i Canvas.

Canvas-tokenet sendes aldri til lagringstjenesten filene lastes opp til, bare til
Canvas selv. Tokenet må tilhøre en lærer med rett til å endre sider og filer i emnet.

API: `PUT /api/canvas-player/design`, `POST /api/canvas-player/preview`,
`POST /api/canvas-player/runs` (`{"action": "publish" | "remove", "module_ids": [...]}`)
og `GET /api/canvas-player/runs/current`.

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
| `SECRET_KEY` | nei (lages automatisk) | Signerer session-cookien og krypterer lagrede OAuth-tokens |
| `HTTPS_ONLY` | nei | `1` bak HTTPS, slik at session-cookien får `Secure` |
| `DATA_DIR` | nei | Hvor SQLite-filen ligger (standard `./data`) |
| `OUTPUT_DIR` | nei | Hvor lydfilene havner (standard `./output`) |
| `PRICING_FILE` | nei | Alternativ prisfil |
| `DATABASE_URL` | nei | Overstyr hele databasetilkoblingen |
| `LOG_LEVEL` | nei | `DEBUG`/`INFO`/`WARNING` (standard `INFO`) |
| `HTTP_TIMEOUT` | nei | Sekunder, standard 60 |

Er `SECRET_KEY` ikke satt, lager appen en nøkkel første gang den starter og
lagrer den i `.env`, slik at innlogging og SoundCloud-tilkoblingen overlever en
omstart. Byttes nøkkelen senere, må du koble til SoundCloud på nytt. På en server
bør du sette den selv som miljøvariabel:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Kan `.env` ikke skrives (for eksempel i en skrivebeskyttet container), brukes en
tilfeldig nøkkel per prosess. Appen logger da en advarsel om at innlogging og
SoundCloud-tilkobling ikke overlever en omstart.

### 🔑 API-siden skriver `.env`

GUI-et har ett skille som er verdt å kjenne: **🔑 API skriver `.env`** (adresser,
nøkler og app-passord), mens **⚙️ Innstillinger skriver databasen** (stemme,
modell, budsjett, filnavnstil og deling). Stemmelista hentes med den *lagrede*
ElevenLabs-nøkkelen, så legg inn nøkkelen først, og velg stemme etterpå.

Lagrer du nøkler i GUI-et, skrives de til `.env` i prosjektmappa. Skrivingen er
atomisk (temp-fil + `os.replace`) og filen får restriktive rettigheter der OS-et
støtter det. **Dette er ment for lokal eller privat drift.** På en server bør du
bruke vanlige miljøvariabler; de vinner over filen, og GUI-et vil da nekte å
endre dem og si fra om det.

---

## Skaffe API-nøkler

Alle nøklene legges inn under **🔑 API** i menyen, med en «Test tilkoblingen»-knapp
per leverandør.

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

### Med dobbeltklikk (Windows)

Dobbeltklikk `start.bat` i prosjektmappa. Den starter appen og åpner nettleseren
så snart appen svarer. Lukk konsollvinduet for å stoppe den.

Vil du heller ha et ikon på skrivebordet, kjør dette én gang:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\create-desktop-shortcut.ps1
```

Det lager snarveien «Canvas til lyd» med appens ikon. `-ExecutionPolicy Bypass`
gjelder bare for den ene kjøringen og endrer ingen innstillinger på maskinen.

- Kjører appen allerede, åpner den bare nettleseren i stedet for å starte en til.
- Er port 8000 opptatt av et annet program, sier den fra. Velg da en annen port:
  `start.bat --port 8001`.
- Den starter uten `--reload`, som er ment for vanlig bruk. `--reload` (under)
  er for når du endrer koden.

### Fra terminalen

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

## ⬆️ Oppdatering

Menypunktet **⬆️ Oppdatering** viser hvilken kode som kjører, og henter ny når du
ber om det.

Siden viser tre ting: programversjon og commit (både den som kjører og den som
ligger i mappa), git-tilstanden, og databaseskjemaet. Er commit-en som kjører en
annen enn den i mappa, sier siden at appen må startes på nytt.

**«Se etter oppdatering»** kjører `git fetch` og forteller hva som er nytt.
**«Oppdater nå»** gjør «fast forward» av `origin/<gren>`. Den stopper heller enn å
overskrive noe:

- Lokale endringer i sporede filer stopper oppdateringen, og filene listes opp.
  Filer som ikke ligger i git (som databasens WAL-filer) påvirker ingenting.
- Egne commits foran origin, løsrevet HEAD eller manglende oppstrømsgren stopper
  den også, hver med sin egen forklaring.
- Ingenting tvinges, og `git reset` brukes aldri. Lydfiler, `.env` og databasen
  røres ikke.
- Endres avhengighetene (`pyproject.toml` og venner), sier siden at du må kjøre
  `install.bat` før du starter appen igjen.

**Appen starter seg ikke selv på nytt.** Den kjører i et konsollvindu uten
tjeneste eller vaktprosess, og `app/launcher.py` nekter å starte mens porten er
opptatt. En automatisk omstart kunne derfor endt med at appen ble borte uten at du
så hvorfor. Siden forteller i stedet hva du gjør: lukk vinduet, og dobbeltklikk på
snarveien.

**«Databaseskjema»** er ikke et migrasjonsnummer. Prosjektet har ingen nummererte
migrasjoner: `add_missing_columns()` legger til nye kolonner ved oppstart. Derfor
viser siden tre fakta i stedet for ett — versjonen koden forventer
(`SCHEMA_VERSION` i `app/database.py`, satt for hånd), versjonen som står i selve
databasefilen (`PRAGMA user_version`), og hvilke kolonner siste oppstart faktisk
la til. De to siste er alltid sanne, uansett om noen glemmer å øke tallet.

Kjører du i Docker, finnes det ingen `.git` i imaget. Siden sier da tydelig fra at
den ikke kan oppdatere seg selv, og du henter ny versjon som et nytt image – se
[GitHub og deployment](#github-og-deployment).

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

### Manuell tilgjengelighetstest

`tests/test_accessibility.py` regner ut kontrastforholdene fra `app.css` og leser
den ferdige HTML-en, men noen ting må prøves med tastatur og skjermleser:

- Trykk Tab øverst på `/canvas`: første stopp skal være «Hopp til hovedinnhold».
- Tab videre til en modul: Enter og mellomrom skal åpne den, og pilen skal snu.
- Åpne kostnadsdialogen: Tab skal bli værende inne i dialogen, Escape skal lukke
  den, og fokus skal tilbake til knappen du kom fra.
- Slå på «reduser bevegelse» i Windows: varsler skal slutte å gli inn, men
  fremdriftssnurren skal fortsatt snurre.
- Med skjermleser: et varsel skal leses opp, og menylenkene skal ha navn også når
  vinduet er smalere enn 520 px og bare emojiene vises.

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
  `tracks` som `{"urn": "soundcloud:tracks:N"}`; oppdatering med
  `PUT /playlists/{playlist_urn}` der hele sporlisten sendes på nytt. Numeriske
  `id`-felt er utfaset fordi ID-ene ikke lenger får plass i 32 bit, og SoundCloud
  avviser `{"id": N}` med «Could not parse JSON request body»
  ([soundcloud/api#448](https://github.com/soundcloud/api/issues/448)).

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

### Automatisk publisering og eksisterende spillelister

På ☁️ SoundCloud-siden kan du slå på **«Publiser automatisk når en hel modul er
ferdig generert»**. Når en genereringsjobb er ferdig og alle sidene med tekst i en
modul har oppdatert lyd, lastes sporene opp og modulens spilleliste opprettes
eller oppdateres. En modul som bare delvis er generert, publiseres ikke
automatisk; bruk «Publiser nå» på modulen hvis du vil publisere den likevel.
Publiseringen kan aldri stoppe eller feile selve lydgenereringen, og resultatet
vises på SoundCloud-siden.

Hver modul kan kobles til en spilleliste du allerede har på SoundCloud, i stedet
for at appen lager en ny. Appen fjerner aldri spor fra en spilleliste: sporene
som ligger der fra før beholdes, modulens spor settes inn i Canvas-rekkefølge, og
et spor som er lastet opp på nytt fordi lyden er regenerert, byttes ut på samme
plass. Den gamle versjonen av sporet blir liggende på SoundCloud-kontoen din.

Før et spor gjenbrukes, sjekker appen at det fortsatt finnes på SoundCloud-kontoen.
Har du slettet spor der, for eksempel sammen med en spilleliste, lastes de opp på
nytt neste gang modulen publiseres. Bare et tydelig «finnes ikke» fra SoundCloud
fører til ny opplasting, så en midlertidig feil gir aldri doble spor.

API-dokumentasjonen sier ikke om `PUT /playlists/{playlist_urn}` erstatter eller legger til
spor. Appen sender derfor alltid hele den sammenslåtte sporlisten, sjekker etterpå
at spillelisten på SoundCloud stemmer, og endrer ikke en spilleliste hvis
SoundCloud ikke oppgir alle sporene i den.

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
- **Oppdatering uten inndata.** `/api/update/*` tar ingen felter i det hele tatt:
  gren og fjernlager leses fra repoet, git kjøres med argumentliste uten skall,
  med tidsavbrudd og uten innloggingsdialoger, og bare «fast forward» er tillatt.
  Endepunktene krever innlogging når `APP_PASSWORD` er satt. Står maskinen der
  andre nettverk når den, bør du sette et passord.
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
| GET | `/api/canvas/sync/status` | Fremdrift for synkroniseringen som kjører |
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
| GET | `/api/update/status` | Versjon, commit, git-tilstand og databaseskjema |
| POST | `/api/update/check` | `git fetch` – ser etter ny kode |
| POST | `/api/update/apply` | «Fast forward» av origin, stopper ved lokale endringer |
| GET | `/api/costs/usage` | Faktisk forbruk |
| GET | `/api/elevenlabs/voices` | Tilgjengelige stemmer |
| GET | `/api/elevenlabs/models` | Tilgjengelige modeller |
| POST | `/api/elevenlabs/test` | Test nøkkelen |
| GET | `/api/elevenlabs/voices/{id}/preview` | Lydprøve av en stemme (hentes av appen, ikke nettleseren) |
| GET | `/api/audio/{page_id}` | Strøm lydfilen (`?download=1` for nedlasting) |
| GET | `/api/soundcloud/status` | Tilkoblingsstatus |
| POST | `/api/soundcloud/connect` | Start OAuth |
| GET | `/api/soundcloud/callback` | OAuth-retur |
| POST | `/api/soundcloud/disconnect` | Koble fra |
| POST | `/api/soundcloud/upload` | Last opp lydfiler |
| POST | `/api/soundcloud/sync-playlists` | Én spilleliste per modul |
| GET | `/api/uttale` | Uttalelisten |
| PUT | `/api/uttale` | Lagre listen og rense de lagrede sidene på nytt |
| POST | `/api/uttale/prov` | Vis hvordan en tekst blir lest |
| GET | `/api/settings/status` | Maskert nøkkelstatus |
| POST | `/api/settings` | Lagre innstillinger |
| POST | `/api/settings/test/{provider}` | Test én leverandør |

Interaktiv dokumentasjon: <http://localhost:8000/api/docs>.

---

## Feilsøking

**«Activate.ps1 cannot be loaded because running scripts is disabled on this
system»** – Windows blokkerer PowerShell-skript som standard. Du trenger ikke
aktivere miljøet: bruk `install.bat`, eller kall Python i `.venv` direkte, for
eksempel `.\.venv\Scripts\python.exe -m pip install -e .`. Du trenger ikke
endre execution policy på maskinen.

**«Fant ikke Python» fra install.bat** – installer Python 3.11 eller nyere fra
<https://www.python.org/downloads/> med «Add python.exe to PATH» avhuket, og
kjør `install.bat` på nytt.

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
| `app/static/js/settings.js` | Delt lagre-, test- og fjern-logikk for 🔑 API og ⚙️ Innstillinger |
| `app/services/update_service.py` | All git-tilgang, ett sted, uten skall og uten inndata |
| `app/static/css/app.css` | Designsystemet (tokens, komponenter, responsivt) |
| `tests/test_pipeline.py` | Ende-til-ende-beviset for hele kjeden uten betalte kall |
