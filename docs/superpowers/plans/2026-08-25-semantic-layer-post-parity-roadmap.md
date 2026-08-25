# Sémantická vrstva: co dál po dokončení parity (v2, korigováno)

**Datum:** 2026-08-25
**Stav:** návrh k odsouhlasení, pre-implementace
**Ověřeno proti:** `main` @ 937556a37 (v0.87.0)
**Nahrazuje:** ústní/chatový plán v2 (argumentováno proti `d4fb0c6`, 126 commitů zpět)
**Navazuje na:**
- `docs/superpowers/specs/2026-08-13-open-semantic-layer-contract-design.md` (kontrakt úložiště)
- `docs/superpowers/specs/2026-08-14-semantic-layer-ui-and-agent-parity-design.md` (UI + agentní parita, schváleno)
- `docs/superpowers/plans/2026-08-16-semantic-layer-parity-sequencing.md` (vlny 0–4 — **kompletně doběhly**, viz níže)
- `docs/superpowers/specs/2026-08-24-semantic-layer-chat-authoring-design.md` (chat-first authoring, implementováno)

Tenhle dokument neotvírá znovu principy původního zadání — ty se auditem
nezpochybnily. Opravuje **šest faktů**, na kterých zadání stálo, protože
mezi jeho verifikačním commitem a `main` leží 126 commitů a značná část z nich
je právě dokončení sequencing plánu z 2026-08-16. Dvě z oprav mění rozsah
fáze, ne jen formulaci (F1, F5).

---

## 0. Korekce vstupního obrazu

**K0.1 — vlny 0–4 + f2-krok-1 ze sequencing plánu (2026-08-16) jsou hotové.**
Zadání v2 je psalo jako budoucí práci nebo o nich nevědělo. Ověřeno commity
a kódem:

| Vlna | Stav | Důkaz |
|---|---|---|
| 0 (pravdivost povrchů) | **hotovo** | `ea1dc6413`; coverage přes všechny modely `connectors/keboola/semantic_layer.py:1499-1510`; `grain: Optional[str] = None` v `src/repositories/metrics.py:50` i `metrics_pg.py:43` |
| 1 (parita projektoru + identita) | **hotovo** | `project_document` čte `AGNES` extension a skládá runnable SQL (`src/semantic/projection.py:76-205`); adaptér stamphuje `metastore_id`/`metastore_revision` (`connectors/keboola/semantic_ossie.py:90-112`) |
| 2 (shadow → cutover) | **hotovo** | `0d4ce9f9c`, `e0fecec07`; `connectors/keboola/semantic_layer.py:607-880` je jediný writer pod `source="keboola_metastore"`; `tests/test_semantic_layer_cutover_parity.py` pinuje, že legacy composer je pryč |
| 3 (`validate_semantic_query`) | **hotovo** | `9a30ddb9c`; REST `POST /api/semantic-models/validate-query` (`app/api/semantic_models.py:583-604`), CLI `agnes semantic-model validate-query`, MCP `validate_semantic_query` (`foundation_tools.py:476-522`), fail-closed bez validního modelu |
| 4.1–4.2 (agent parita + browse UI) | **hotovo** | `f474790ca`, `8b21600ab`; `get_semantic_context` a `get_semantic_schema` typované/scoped (`app/api/semantic_models.py:616-673`, MCP `foundation_tools.py:528-599`), skill `semantic-layer-building`, 5 tabů v `semantic_layer_detail.html`, `ai`/anti-keywords blok (`semantic_layer_view.py:262-343`) |

Tohle **zásadně zužuje rozsah F1** (viz K0.3) a **eliminuje** větev "postavit
read parity" z F4/F5 argumentace — čtecí trojice a validátor už existují a
fungují přes REST/CLI/MCP shodně.

**K0.2 — CLAUDE.md dnes nese víc než boolean flag, ale pořád nic fyzicky
na disku.** Původní audit tvrdil "jen bool `has_models`" — nepřesně.
`config/claude_md_template.txt:47-72` má celou sekci popisující sémantickou
vrstvu jako "authoritative source of business meaning" s instrukcemi pro
agenta, gated na `semantic_layer.has_models` (`src/claude_md.py:280`). Co
tam **není**: žádný fyzický soubor s dokumentem na disku, žádný
`generated_at`/`content_hash`/`ttl`, žádný jednořádkový katalog modelů
jménem. Agent dnes sémantiku *čte živě* (CLI/MCP proti serveru), nikdy ji
nemá lokálně cachovanou. To je přesně mezera, kterou princip 5 (fyzická
cache s TTL) řeší — zůstává v platnosti, jen je užší, viz F1 níže.

**K0.3 — Databricks legacy writer existuje a musí se nahradit stejným
postupem jako Keboola, ne postavit od nuly.** `connectors/databricks/
semantic_layer.py::sync_semantic_layer` dnes promítá Unity Catalog metric
views přímo do `metric_definitions` pod `source='databricks_semantic_layer'`,
mimo dokumentovou cestu. Původní F0 už počítala s "cutover se smazáním
plochého zapisovatele, parity test po vzoru Keboola" — to je správně a
zůstává. Doplnění: postup by měl **doslova kopírovat** vlnu 2 (shadow write
pod `source='databricks_metrics'` vedle legacy, golden diff na nulu, pak
jedna transakce smaž+zapiš), protože tenhle playbook je teď ověřený a
zdokumentovaný (`docs/superpowers/plans/2026-08-16-...md` §1, §3 vlna 2) —
není důvod ho pro Databricks znovu vymýšlet.

**K0.4 — Fáze 5 (auto-build) je z většiny hotová, ale jinak, než zadání
navrhovalo — a nový návrh je s hotovou verzí v přímém konfliktu.**
`docs/superpowers/specs/2026-08-24-semantic-layer-chat-authoring-design.md`
je implementováno (`f53285240` a navazující commity): `POST /api/
semantic-models/apply` s admin/non-admin větvením, Studio doména
`semantic-layer` (ne "semantics", jak navrhovalo zadání), fronta
`authoring_suggestions`, chat profil `semantic-model-builder`. Design
dokument explicitně řeší otázku, kterou si F5 v zadání teprve kladla, a
odpovídá **opačně**: viz jeho sekce *Non-goals* — *"A deterministic scaffold
engine. The chat agent is the scaffolder — it reads catalog/schema/samples
... and drafts the document itself; a human reviews in chat before apply,
every time."* Zadání F5 chtělo přesně tohle: deterministický scaffold +
LLM návrhová vrstva + `GEN`/`DRAFT`/`KEEP` klasifikace + `status='draft'`
dokument mimo review frontu. Stavět to vedle existujícího chatového apply
by znamenalo dvě autorské cesty do stejné cílové tabulky. **F5 se přepisuje**
— viz níže.

**K0.5 — Fáze 4.1 (pokrytí) má na čem stavět, ne od čeho začít.**
`GET /api/admin/semantic-layer/coverage` (`app/api/
keboola_semantic_layer_refresh.py:223-252`, `connectors/keboola/
semantic_layer.py::compute_semantic_coverage`) + CLI `agnes admin
semantic-layer coverage` existují — ale jen pro Keboola Metastore binding
coverage (kolik metrik/objektů z jednoho zdroje se namapovalo na
registrované tabulky). Neřeší napříč zdroji otázku "která registrovaná
tabulka nemá vůbec žádný model". F4.1 se **zobecňuje**, nestaví se znovu.

**K0.6 — scaffold (`src/data_semantics_scaffold.py`) zůstává nedotčený a
čte ploché tabulky.** Potvrzeno beze změny oproti prvnímu auditu
(`data_semantics_scaffold.py:1-59`: čte `metric_definitions`,
`table_registry`, `column_metadata`, `bq_metadata_cache`). Otázka, co s ním,
se ale mění — viz F5.

**K0.7 — cílových providerů je v byznysové poptávce víc, než kolik dnes
pokrývají adaptéry.** Vedle Keboola/Snowflake/Databricks se v rané diskuzi
objevily i další platformy sémantické/katalogové vrstvy (Collibra — už
zmíněná v principu 1 — a Dawiso). Žádná fáze v tomhle plánu nový adaptér pro
ně nescopuje; kontrakt (`extract(config) -> list[str]`) je pro dalšího
providera aditivní, takže se přidává podle poptávky, ne podle tohoto plánu.

**K0.8 — provider-grant (F2) má známé omezení, které plán dosud mlčky
přecházel.** Grant skupině na celého providera je all-or-nothing: dnešní
návrh neumí ze skupiny, která už providera vidí, vyjmout jen část jeho
modelů. To je vědomý důsledek principu 4 ("nejhrubší granularita, která
řeší reálný požadavek"), ne přehlédnutá mezera — ale F2 by to měl explicitně
pojmenovat jako known limitation, ne nechat objevit až v review. Jemnější
dělení (odvození N sub-vrstev z jednoho providera, nezávisle syncovaných a
grantovatelných skupinám) je reálně navrhovaný směr pro případ, že by tohle
omezení v praxi vadilo — viz otevřená otázka 8.

**K0.9 — pokrytí/health (F4) je jeden vstup do širší, dosud nescopované
myšlenky.** V rané diskuzi padl nápad na "instance completeness score"
napříč sémantikou, metrikami, skilly, specializovanými agenty, knowledge
base a glosářem — ve stylu gamifikace (dokonči tohle a tohle, zvedneš skóre
o X %). F4.1/4.2 dodávají přesně jeden vstup do tohodle většího konceptu
(pokrytí sémantiky), ale samotný gamifikovaný skóre napříč doménami je mimo
rozsah tohoto plánu — nechává se jako navazující iniciativa, netiše se sem
nevkládá.

**K0.10 — externí evaluace bývají tvrdší na grounding a governance než na
funkční pokrytí.** Nezávisle na tomto repu proběhla úvaha o tom, jak se
platforma jako Agnes obhajuje proti holé context-engineered baseline (dobře
sestavený systémový prompt/seed pack bez platformy) — a typický vzorec
takové evaluace je: (a) je nutné prokázat, že platforma přidává hodnotu nad
rámec samotného kontextu, ne jen že "taky odpoví", a (b) jakýkoli únik dat
mimo oprávnění nebo fabrikované tvrzení je tvrdá nula bez ohledu na kvalitu
zbytku odpovědi — žádná škála, žádné "skoro". Pro tenhle plán to znamená:
F1 (agent má sémantiku fyzicky, ne jen jako prózu v promptu) a F4.4 (agent
se u chybějící sémantiky ptá, nehádá, a nepodloženou odpověď označí) nesou
riziko/hodnotu neúměrně vyšší, než jejich velikost (S) napovídá — jsou to
přesně ty vlastnosti, které bez platformy nejde replikovat pouhým
kontextovým inženýrstvím. F2/F3 (RBAC granularita, detach-a-uteklo
viditelnost) analogicky nesou riziko na governance straně: nekonzistentní
nebo neúplný grant je přesně tvar chyby, který takové hodnocení penalizuje
tvrdě a bez odstupňování. Viz přeuspořádané doporučení v §4.

---

## 1. Principy a argumentace

Beze změny proti zadání — audit nezpochybnil žádný z nich, jen zpřesnil,
kolik infrastruktury pro ně už existuje:

1. **Ossie jako společný jazyk, adaptéry jako překladače.** Platí; adaptérů
   je dnes reálně 5: `native`, `keboola_metastore`, `snowflake_semantic`
   (`src/semantic/adapters/__init__.py:43-53`) a Keboola/Databricks legacy
   direct-writes, které F0 ruší.
2. **One-way + export jako pojistka.** Platí beze změny; export hotový
   (`app/api/semantic_models.py:503-516`, CLI `admin semantic-model export`).
3. **Oprava cizí sémantiky = detach (danger akce).** Platí beze změny;
   `409 source_owned` dnes flat (viz F3), detach zůstává navrhovaná
   nadstavba.
4. **Granularita přístupu = provider.** Platí beze změny; `SEMANTIC_SOURCE`
   potvrzeně chybí (`app/resource_types.py:34-58`).
5. **Sémantika fyzicky u agenta, cache s TTL.** Platí, ale cíl je užší, než
   zadání předpokládalo — živá čtecí vrstva (get_semantic_context/schema,
   validate-query) je hotová; chybí jen fyzická distribuce do workspace.
6. **Agnes si kryje záda: kvalita viditelná všude.** Platí; realizace se
   opírá o existující, ne nový coverage engine (K0.5).
7. **Feedback smyčka.** Beze změny, potvrzeně 100% net-new.

---

## 2. Co v Agnes existuje (přesně, k `main`@937556a37)

- Dokumentový store + 5 adaptérů, hash-skip sync, izolovaný prune per
  `(source, source_ref)`: `src/semantic/importer.py`, `src/semantic/
  adapters/`.
- Export do Ossie (CLI + REST): hotovo.
- RBAC: granty per data package (`data_package_semantic_models`) i per model
  (`ResourceType.SEMANTIC_MODEL`, `_can_read_model`,
  `app/api/semantic_models.py:116-137`). Provider-level grant chybí.
- **Kompletní čtecí + validační trojice, živě, RBAC-scoped, na REST i CLI i
  MCP**: `get_semantic_context`, `get_semantic_schema`, `validate_semantic_
  query` (vlny 3–4.1). Nic z toho se nedistribuuje na disk analytika.
- Browse UI `/semantic-layer` s pěti taby, `ai`/anti-keywords blokem,
  read-only pro importované modely (vlna 4.2).
- **Chat-first authoring**: `POST /api/semantic-models/apply`, Studio doména
  `semantic-layer`, fronta `authoring_suggestions`, chat profil
  `semantic-model-builder`. Admin zapisuje rovnou, ne-admin jde do fronty.
  Žádný "draft" status mimo frontu, žádná deterministická generace.
- Keboola-specific binding coverage endpoint + CLI (K0.5).
- `semantic_sources.last_sync_at/status/error` v schématu (zdravotní
  suroviny, dosud neagregované).
- Databricks: legacy přímý zápis (`sync_semantic_layer`), mimo dokumentovou
  cestu — jediný adaptér, který ještě chybí (F0).
- Scaffold čte ploché tabulky, generuje `_brief.md`/`tables/*.yml`/
  `metrics/*.yml`/`glossary.md` — použití dnes nejasné/zastaralé vzhledem
  k chat-authoringu (viz F5).

**Chybí skutečně:** Databricks Ossie adaptér, provider granty, detach
mechanismus, fyzická TTL distribuce do workspace, cross-source coverage,
health agregace, mute-s-podpisem, feedback tabulka, propojení
coverage→authoring.

---

## 3. Plán ve fázích (revidováno)

### Fáze 0 — Databricks na dokumentovou cestu *(M, beze změny rozsahu)*

Postup kopíruje ověřený Keboola playbook (K0.3): nový
`connectors/databricks/semantic_ossie.py` (fetch: `information_schema.tables`
→ `METRIC_VIEW`, tělo `SHOW CREATE TABLE`, YAML mezi `$$`) → shadow write pod
`source='databricks_metrics'` vedle legacy → golden diff na nulu → jedna
transakce cutover (smazat `source='databricks_semantic_layer'`, projektor
přebírá) → smazat `sync_semantic_layer`. `MEASURE()` výrazy tagovat výhradně
databricks dialektem, aby je `validate-query` (už hotový, vlna 3) korektně
označil jako lokálně nespustitelné. Guard na kolizi jmen metrik napříč zdroji
(`metric_definitions.name` nemá unique constraint — týká se i Snowflake).
Mimochodem: migrace `source_ref` do `column_metadata` (vzor 0054).

**DoD:** Databricks metriky v dokumentech, `sync_semantic_layer` smazán,
`agnes catalog --metrics` vrací stejný tvar (`table_name` + runnable SQL)
napříč Keboola/Snowflake/Databricks, export i `validate-query` fungují.

### Fáze 1 — distribuce jako fyzická cache s TTL *(S, rozsah zúžen K0.1/K0.2)*

Živá čtecí vrstva už existuje a se neduplikuje. Fáze 1 řeší jen materializaci
na disk:

1. Nový render krok v `agnes pull`: pro RBAC-viditelné validní modely
   (stejná brána jako `_can_read_model` + package granty) zapsat dokument
   jako soubor do workspace (layout `_brief.md` / `tables/*.yml` /
   `metrics/*.yml` / `glossary.md` — recyklovat z `data_semantics_scaffold.py`
   jen renderovací část, ne jeho čtení z plochých tabulek).
2. Hlavička na souboru: `generated_at`, `content_hash` (existující sloupec
   `semantic_models.content_hash` — recyklovat, ne vymýšlet), `source_slug`,
   `ttl`. Soubory read-only (chmod).
3. TTL politika **doplněná do existující sekce** `config/
   claude_md_template.txt:47-72` (ne nová sekce) — do vypršení agent věří
   souboru; po vypršení instrukce velí ověřit přes `get_semantic_context`
   (hash už do jeho odpovědi patří nebo se doplní), `validate-query` vždy
   serverová beztak.
4. Jednořádkový katalog modelů (jméno + popis) do stejné CLAUDE.md sekce —
   dnes tam je jen autoritativní prosa, ne výčet.

**DoD:** po pullu má analytik dle svých práv slovník fyzicky na disku;
zastaralá cache se pozná z hlavičky a CLAUDE.md agentovi řekne, co s tím.

### Fáze 2 — víc vrstev, přístup per provider *(S–M, beze změny)*

1. Nový grantovatelný resource `SEMANTIC_SOURCE`; grant skupině na zdroj →
   čtení všech jeho modelů, vrstvený pod existující package/model granty.
2. `_can_read_model` rozšířit o třetí větev; F1 render i CLAUDE.md katalog
   ji zdědí automaticky (stejná brána).
3. UI: u zdroje seznam skupin s přístupem; u modelu vidět, odkud přístup
   pochází.

**DoD:** finance vidí finanční provider, HR svůj; soubory ve workspace to
respektují. **Explicitně zdokumentovat jako known limitation** (K0.8): grant
je all-or-nothing na celého providera, nejde z něj skupině vyjmout jen
některé modely — pokud se to ukáže jako blokující, řešením je otevřená
otázka 8 (sub-vrstvy), ne rozšiřování F2 o výjimky ad hoc.

### Fáze 3 — detach & override + export *(M, beze změny)*

1. `semantic_models.sync_mode`: `synced` (default) | `detached`, plus
   `detached_at/by`, `detach_base_hash`.
2. Editace source-owned modelu: dnešní flat `409 source_owned`
   (`app/api/semantic_models.py`, více guardů napříč endpointy) rozšířit o
   danger-flow potvrzení → kopie dokumentu, `detached`, audit.
3. Sync odpojený model nepřepisuje, ale porovnává hash → indikátor "zdroj
   se od odpojení změnil".
4. Re-attach: danger akce s náhledem, co se zahodí; audit.
5. Export odpojeného modelu zviditelnit v UI vedle detach flow (mechanismus
   sám existuje).

**DoD:** klient opraví cizí sémantiku, systém nikdy nemlčí o odpojení a
uteklém zdroji; návrat je jedno tlačítko s náhledem.

### Fáze 4 — kvalita a krytí zad *(M, 4.1 zobecněná, zbytek beze změny)*

1. **Pokrytí (zobecnit, ne stavět):** rozšířit/obalit existující Keboola
   binding-coverage engine (K0.5) o cross-source dotaz: registr tabulek
   (data packages) vs. tabulky referencované ve *všech* valid dokumentech
   napříč zdroji. Endpoint + CLI `agnes semantic-model coverage` (odlišit
   jménem od stávajícího Keboola-specific `admin semantic-layer coverage`)
   + badge v UI.
2. **Health check:** agregát nad `semantic_sources.last_sync_status/at`,
   počtem odpojených modelů s uteklým zdrojem (závisí na F3), chybami
   validace dokumentů, pokrytím z 4.1. Jeden endpoint pro UI banner, CLI,
   MCP.
3. **Vypnutí kontroly = podpis:** mute per instance/zdroj s uloženým
   kdo/kdy/co, viditelné v health výstupu.
4. **Chování agenta:** ověřit, co `config/claude_md_template.txt:47-72` už
   říká (autoritativnost, canonical-metric-first), a **doplnit** jen chybějící
   pravidla — explicitní "zeptej se, nehádej" mimo slovník a "odpověď bez
   opory v sémantice označ". Nepsat sekci od nuly. E2E test konverzace.
5. **Feedback:** tabulka `semantic_feedback` (otázka, SQL, metrika, hash
   verze modelu, komentář, kdo/kdy) + MCP tool `flag_semantic_issue` + admin
   fronta — nejpřirozeněji jako doména ve Studiu vedle `semantic-layer`
   (fronta `authoring_suggestions` je pro návrhy modelů, feedback potřebuje
   vlastní tabulku, ale může sdílet Studio UI vzor).

**DoD:** admin na jedné obrazovce vidí, co nemá sémantiku napříč zdroji, co
se nesynchronizuje/je odpojené-a-uteklo, co lidi hlásí.

### Fáze 5 — auto-build: napojit existující chat authoring, nestavět nový *(S, přepsáno z L)*

Zadání navrhovalo deterministický scaffold + LLM draft vrstvu + `status=
'draft'` dokument + `GEN`/`DRAFT`/`KEEP` regenerace + auto-trigger. To je
v přímém konfliktu s implementovaným designem (K0.4), který zvolil
opačně: žádný deterministický scaffold, chat agent *je* scaffolder, review
vždy před apply. Nová fáze 5 respektuje tohle rozhodnutí a řeší jen chybějící
propojku:

1. **Trigger:** nová tabulka v registru / nový data package s 0% pokrytím
   (napojení na F4.1) ⇒ notifikace/CTA směrem k existujícímu
   `/admin/studio/semantic-layer` s předvyplněným kontextem (jméno tabulky,
   schéma, proč se to zobrazilo) — **ne** automaticky vygenerovaný draft
   dokument mimo review.
2. **Scaffold modul:** rozhodnout explicitně, ne předpokládat. Buď (a)
   `data_semantics_scaffold.py` zrušit — jeho práci dnes dělá chat agent
   lépe (čte živá data, ne cache) — nebo (b) zúžit na jediný ospravedlnitelný
   use-case, který chat-authoring nepokrývá (např. plně offline/bulk
   generace bez lidské interakce pro desítky tabulek najednou). Toto je
   otevřená otázka pro produktové rozhodnutí, ne implementační detail —
   viz níže.
3. Pokud (b): scaffold přesměrovat na výstup v Ossie *jako draft-only vstup
   do stejné chat-authoring session* (ne jako samostatná cesta k `valid`
   dokumentu), aby nevznikla druhá autorská cesta do `semantic_models`.

**DoD:** napojení dat bez sémantiky vede k viditelné výzvě směrem k existující
chat-authoring ploše; žádná nová zápisová cesta do `semantic_models` vedle
`/apply`.

---

## 4. Pořadí a závislosti

```
F0 (Databricks)      — hned, blokuje DBX obsah všude, kopíruje hotový playbook
F1 (Cache + TTL)     — paralelně s F0, rozsah zúžen (jen distribuce, ne čtení)
F2 (Provider granty) — po F1 (render dědí brány), malá
F3 (Detach)          — nezávislá na F1/F2, kdykoli po F0
F4.1 (Pokrytí)       — nezávislá, může jít hned (zobecnění existujícího)
F4.2-4.5             — po F1 (staleness) a F3 (odpojené-a-uteklo)
F5                   — po F4.1 (trigger); rozhodnutí o scaffoldu nezávislé, kdykoli
```

Závislostní graf beze změny, ale K0.10 mění, čemu dát přednost při stejné
velikosti sousta: **F1 a F4.4 táhnout dřív, ne až v přirozeném pořadí**,
protože nesou nepoměrně víc rizika/hodnoty než jejich (S) velikost napovídá
— jsou to vlastnosti, které se nedají nahradit lepším promptem. F2/F3 zůstávají
ve stejném pořadí, ale jejich test před vyhlášením "hotovo" by měl zahrnovat
záměrný pokus o únik (grant skupině bez přístupu, ověřit 100% odmítnutí),
ne jen šťastnou cestu.

Doporučení: **F0 + F1 + F4.1 paralelně**, s F4.4 (agent behavior pravidla v
CLAUDE.md) vytažené do stejné vlny, i když formálně visí na F1 — je to
textová změna bez závislosti na fyzické distribuci, jen na existující
CLAUDE.md sekci (K0.2), takže může jet souběžně, ne až po F1 dokončení. Pak
F2 + F3 (s RBAC-negativním testem výše) → F4.2-4.5 → F5. Wow efekt vzniká
kombinací F1 (agent má slovník fyzicky) + F4.1 (vidí mezery) + F5 (mezera
vede k existující chatové autorské ploše, ne k nové); důvěryhodnost navenek
vzniká kombinací F1+F4.4 (grounding) + F2/F3 bez úniku (governance).

---

## 5. Otevřené otázky

1. Default TTL cache (24 h? per zdroj podle rozvrhu syncu?) — beze změny.
2. Kdo smí ztlumit health kontrolu — admin only, nebo vlastník zdroje? —
   beze změny.
3. Kde přesně se hlásí špatná odpověď (MCP tool jasný; UI chatů klienta?) —
   beze změny.
4. **Nová:** scaffold (`data_semantics_scaffold.py`) — zrušit, nebo zúžit na
   bulk-offline use-case? Rozhoduje produkt, ne kód (F5.2).
5. **Nová, analogie ke K0.3 z Keboola sequencingu:** závisí nějaká živá
   instalace na dnešním Databricks `metric_definitions.id` tvaru pod
   `source='databricks_semantic_layer'`? Stejná otázka jako u Keboola
   cutoveru (tam zodpovězeno: žádná FK/RBAC vazba, bezpečné přijmout změnu
   + hint). Pro Databricks to samé ověřit před F0 cutoverem.
6. Práh pokrytí, od kterého "svítí" health check — beze změny, 100% nebude
   realistické.
7. Query logy pro F5 (pokud by přece jen scaffold/offline cesta vznikla) —
   beze změny, nezjištěno.
8. **Nová:** má se stavět odvození N sub-sémantických vrstev z jednoho
   providera (nezávisle syncovaných a grantovatelných podmnožin) jako řešení
   K0.8/F2 limitace, nebo se all-or-nothing provider grant v praxi ukáže
   jako dostatečný? Nerozhodovat teď — sledovat, jestli F2 limitace v reálném
   nasazení vůbec vadí, a teprve pak scopovat.
9. **Nová:** má vazba na širší "instance completeness score" (K0.9) vzniknout
   jako navazující iniciativa hned po F4.1/4.2, nebo zůstat čistě
   hypotetická, dokud o ni nepožádá konkrétní požadavek? F4 endpoint by měl
   být navržený tak, aby šel bez přepisu spotřebovat jako jeden ze vstupů,
   kdyby se na to došlo — ne aby to blokovalo F4 samo.

---

## 6. Argumenty na stůl

- **Proč Agnes a ne nativní vrstva platformy:** beze změny — lock-in
  argument, export existuje.
- **Proč one-way stačí:** beze změny.
- **Proč per-provider přístup:** beze změny.
- **Proč kvalita jako produkt:** beze změny, ale teď stavíme na existujícím
  Keboola coverage enginu, ne na zelené louce — rychlejší cesta k demu.
- **Nový argument (F5):** proč nestavět druhý autorský mechanismus vedle
  chat-first apply — dvě cesty do `semantic_models` by rozbily neplatnost
  invariantu "jen jedna strana zapisovatelná najednou" (princip 3) tím, že
  by vytvořily dva zdroje pravdy o tom, co je "draft". Existující fronta
  `authoring_suggestions` + `apply` endpoint už princip 3 respektuje;
  scaffold-based draft mimo frontu by ho porušil.
