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
- V0 ticket (2026-08-25, tento dokument §7) — první konkrétní, timeboxovaný
  řez roadmapy; opravuje tři věci z korekcí níže (viz K0.11)

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

**K0.11 — V0 ticket (2026-08-25) opravuje tři odhady z tohodle plánu a
přidává novou, dřívější fázi.** Nezávisle vznikl konkrétní, timeboxovaný V0
ticket pro tuhle práci (plné mapování viz §7). Tři korekce vůči textu výše:

1. **Multi-doménový completeness check (K0.9) NENÍ mimo rozsah.** Ticket ho
   staví jako jádro V0 — sémantika/metriky/skill/specializovaný agent/
   knowledge base/glosář v jednom seznamu s dopadem, klikatelné "dodělej
   tohle". Jen % skóre a gamifikace zůstávají V1 (ticket to sám odděluje).
   F4.1 se tím rozšiřuje z "zobecni Keboola coverage" na "postav
   cross-doménový přehled" — viz aktualizovaná Fáze 4.1 a §3a.
2. **Kontrakt pro agenta (root manifest, YAML schéma, progressive
   disclosure) musí být zamrzlý PŘED jakoukoli generací** — F0 i F5 na něm
   stojí doslova, ne jen "hezké mít hotové souběžně". Nová **Fáze K** níže,
   před číslovanou Fází 0.
3. **F0 a Fáze K jsou dvě různé vrstvy a neblokují se navzájem** — F0 řeší
   úložiště (aby každý provider zapisoval do stejných Ossie dokumentů), Fáze
   K řeší, jak z úložiště čte agent na file systému. Obě mohou být "krok 0"
   souběžně. Jediná brzda: přesný scope adaptérové práce (který provider,
   jak velký zásah) čeká na vstup od týmu o tom, co reálně chybí — dokud
   nepřijde, F0 zůstává scope-TBD, ne pevně "Databricks" (viz Fáze 0 níže).

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

### Fáze K — kontrakt pro agenta *(S, nová, blokující — dělá se první)*

Bez tohohle nemá smysl generovat nic (V0 ticket, §7) — určuje tvar, do
kterého F1 renderuje a F5 generuje. Specifikovat a zamrznout:

1. **Adresářová struktura + root entry point** ve workspace, tak aby agent
   poznal, co tam je, bez nutnosti prohledávat celý strom.
2. **Schéma YAMLů** (struktura — ontologie, metriky, vztahy) + Markdown
   (próza — glosář, kontext, kdy co použít), **verzované**, žijící v
   `<DOPLNIT: repo>` — otevřené pole z ticketu, potřebuje rozhodnutí, ne
   odhad.
3. **Pravidla progressive disclosure** — co patří do root manifestu (aby
   agent věděl, kam sáhnout) vs. co až do listu (detail, který se čte jen
   na vyžádání).
4. **MCP/API jako sekundární přístup ke stejnému obsahu** — `get_semantic_
   context`/`get_semantic_schema` (K0.1, vlna 4.1) už existují a zůstávají
   jako fallback/ověřovací cesta, ne primární — primární je file systém.

**DoD:** layout + schéma zdokumentované a zamrzlé pro V0; F1 i F5 se na ně
odkazují, ne vymýšlí formát samy za sebe.

### Fáze 0 — sjednocení providera na dokumentovou cestu *(M, scope TBD — viz K0.11.3)*

Cíl beze změny: každý provider zapisuje do stejných Ossie dokumentů, ne do
staré ploché tabulky vedle nich — dokud aspoň jeden zůstává na starém
formátu, každá další fáze (F1 render, F4.1 completeness) ho musí
speciálně ošetřovat. **Který provider a jak velký zásah je otevřené** —
V0 ticket (§7, bod 2) to výslovně čeká na vstup, co reálně chybí; bez něj
nejde odhadnout, jestli jde o dvoudenní rozšíření, nebo dvoutýdenní práci.
Následující postup platí, ať to dopadne na Databricks nebo jinam:

Postup kopíruje ověřený Keboola playbook (K0.3): nový adaptér (pro
Databricks konkrétně: `connectors/databricks/semantic_ossie.py`, fetch
`information_schema.tables` → `METRIC_VIEW`, tělo `SHOW CREATE TABLE`, YAML
mezi `$$`) → shadow write pod novým `source=` vedle legacy → golden diff na
nulu → jedna transakce cutover (smazat starý flat zápis, projektor přebírá)
→ smazat starý synchronizační kód. Cílový dialekt (pro Databricks:
`MEASURE()`) tagovat výhradně tím zdrojem, aby ho `validate-query` (už
hotový, vlna 3) korektně označil jako lokálně nespustitelný. Guard na kolizi
jmen metrik napříč zdroji (`metric_definitions.name` nemá unique constraint).
**Nově (V0 ticket, §7 bod 2):** cokoli se do Ossie modelu nevejde, jde do
`unmapped[]` a zůstává viditelné — nezahazovat potichu.

**DoD:** provider promítnutý do dokumentů, starý flat zápis smazán,
`agnes catalog --metrics` vrací stejný tvar (`table_name` + runnable SQL)
napříč všemi providery, export i `validate-query` fungují, `unmapped[]`
neprázdný obsah je vidět v UI/CLI, ne jen v datech.

### Fáze 1 — distribuce jako fyzická cache *(S, rozsah zúžen K0.1/K0.2, tvar určuje Fáze K)*

Živá čtecí vrstva už existuje a se neduplikuje. Fáze 1 řeší jen materializaci
na disk, do layoutu, který zamrzla Fáze K:

1. Nový render krok v `agnes pull`: pro RBAC-viditelné validní modely
   (stejná brána jako `_can_read_model` + package granty) zapsat dokument
   jako soubor do workspace (layout dle Fáze K — `_brief.md` / `tables/*.yml`
   / `metrics/*.yml` / `glossary.md` je výchozí návrh, ne finální slovo;
   recyklovat z `data_semantics_scaffold.py` jen renderovací část, ne jeho
   čtení z plochých tabulek).
2. Hlavička na souboru: `generated_at`, `content_hash` (existující sloupec
   `semantic_models.content_hash` — recyklovat, ne vymýšlet), `source_slug`.
   Soubory read-only (chmod).
3. **Invalidace, ne časová TTL** (V0 ticket, §7, upřesňuje princip 5): server
   → analytik synchronizace je hash-based už dnes (importer dělá hash-skip,
   K0.1); tahle fáze jen posouvá stejnou logiku o krok dál — při `agnes pull`
   se soubor přepíše, pokud se `content_hash` liší, bez ohledu na stáří.
   Časová TTL zůstává jen jako **fallback instrukce pro agenta** v CLAUDE.md
   pro dlouho běžící session mezi dvěma pully ("pokud sedíš v jedné session
   déle než X hodin, ověř přes `get_semantic_context`, i když jsi soubor
   nepřepisoval") — ne jako mechanismus, který cokoli maže nebo invaliduje
   sám o sobě. `validate-query` zůstává vždy serverová beztak.
4. Jednořádkový katalog modelů (jméno + popis) do existující CLAUDE.md sekce
   (`config/claude_md_template.txt:47-72`) — dnes tam je jen autoritativní
   prosa, ne výčet.

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

1. **Completeness check, cross-doménový (K0.11.1 — jádro V0, ne jen
   zobecnění):** rozšířit/obalit existující Keboola binding-coverage engine
   (K0.5), ale nezastavit se u semantiky. Pro každý data source vyhodnotit
   napříč doménami, co existuje a co chybí: sémantika (jak dřív), metriky,
   skill, specializovaný agent, knowledge base, glosář — každá doména má
   jiný zdroj pravdy (semantic_models, metric_definitions, marketplace
   registrace, agent profily, corporate memory, glossary_terms), takže
   endpoint agreguje přes víc subsystémů, ne jen přes jeden. Výstup ve V0:
   **seznam s vysvětlením dopadu**, chybějící položka klikatelná ("dodělej
   tohle") — **bez** procentuálního skóre a gamifikace (ty zůstávají V1,
   viz Non-goals §7). Endpoint + CLI `agnes semantic-model coverage`
   (odlišit jménem od stávajícího Keboola-specific `admin semantic-layer
   coverage`) + seznam v UI. Navrhnout endpoint tak, aby šel bez přepisu
   rozšířit o skóre později (otevřená otázka 9).
2. **Health check:** agregát nad `semantic_sources.last_sync_status/at`,
   počtem odpojených modelů s uteklým zdrojem (závisí na F3), chybami
   validace dokumentů, pokrytím z 4.1. Jeden endpoint pro UI banner, CLI,
   MCP.
3. **Vypnutí kontroly = podpis:** mute per instance/zdroj s uloženým
   kdo/kdy/co, viditelné v health výstupu.
4. **Chování agenta + eval harness (rozšířeno V0 ticketem, §7 bod 5):**
   ověřit, co `config/claude_md_template.txt:47-72` už říká (autoritativnost,
   canonical-metric-first), a **doplnit** jen chybějící pravidla — explicitní
   "zeptej se, nehádej" mimo slovník a "odpověď bez opory v sémantice
   označ". Nepsat sekci od nuly. K tomu **eval set v CI**: sada reálných
   otázek + očekávané odpovědi, baseline BEZ sémantiky vs. S sémantikou,
   acceptance `<DOPLNIT: X% → Y%>` — otevřené pole, čeká na práh od
   produktu/zákazníka. Tohle je zároveň test čitelnosti kontraktu z Fáze K:
   ukáže, jestli agent soubory reálně používá, nebo je ignoruje.
5. **Feedback:** tabulka `semantic_feedback` (otázka, SQL, metrika, hash
   verze modelu, komentář, kdo/kdy) + MCP tool `flag_semantic_issue` + admin
   fronta — nejpřirozeněji jako doména ve Studiu vedle `semantic-layer`
   (fronta `authoring_suggestions` je pro návrhy modelů, feedback potřebuje
   vlastní tabulku, ale může sdílet Studio UI vzor).

**DoD:** admin na jedné obrazovce vidí, co nemá sémantiku napříč zdroji, co
se nesynchronizuje/je odpojené-a-uteklo, co lidi hlásí.

### Fáze 5 — auto-generace: chat-authoring agent auto-spuštěný, ne deterministický scaffold *(S, mechanismus rozhodnut V0 ticketem §7 bod 3)*

Zadání v2 navrhovalo deterministický scaffold + LLM draft vrstvu + `status=
'draft'` dokument mimo review frontu. To bylo v přímém konfliktu s
implementovaným designem (K0.4): žádný deterministický scaffold, chat agent
*je* scaffolder, review vždy před apply. V0 ticket (§7 bod 3) chce navíc, aby
"Agnes si sémantiku odvodila sama" pro native-mode datasety bez existující
vrstvy — **rozhodnuto**: sama = ten samý chat-authoring agent, jen spuštěný
systémem, ne až po člověku, který otevře Studio chat. Princip 3 (jedna
zapisovatelná strana) zůstává netknutý, protože výstup jde pořád do stejné
fronty:

1. **Trigger:** nová tabulka v registru / nový data package s 0% pokrytím
   (napojení na F4.1) ⇒ systém **automaticky spustí** existujícího
   `semantic-model-builder` chat agenta nad danou tabulkou (stejná metoda
   survey → schema → draft → validate, co dnes běží po lidském promptu),
   výstup přistane v `authoring_suggestions` frontě přesně jako
   human-initiated návrh. Notifikace pro admina/vlastníka dat je pak "draft
   čeká na schválení", ne "jdi si to sám napsat".
2. **V0 scope:** jen registrované **tabulky**, ne soubory (ticket to
   explicitně zužuje) — auto-trigger na unstrukturovaná data je pozdější
   rozšíření.
3. **Scaffold modul (`data_semantics_scaffold.py`) je teď oddělená otázka**,
   ne blokující: auto-generaci ve V0 řeší auto-spuštěný chat agent, ne
   scaffold. Zda `data_semantics_scaffold.py` zrušit, nebo zúžit na
   ospravedlnitelný bulk-offline use-case (desítky tabulek bez lidské
   interakce, kde by i auto-spuštěný chat agent byl pomalý/drahý), zůstává
   otevřená otázka 4 — ale nebrání dokončení téhle fáze.

**DoD:** napojení dat bez sémantiky vede automaticky k draftu v approval
frontě (ne jen k výzvě, ať to člověk sám napíše); žádná nová zápisová cesta
do `semantic_models` vedle `/apply`.

---

## 3a. Odhad náročnosti

Velikost (S/M/L) v nadpisu fází říká rozsah práce; tahle tabulka přidává
druhou osu — **kde to bolí**, ne jen kolik je toho. Odhady jsou v
člověko-dnech pro inženýra, který kodebázi zná (počítá se s TDD-first a
review, ne s "napsat kód"). `agnes-build` dovoluje nezávislé kousky pustit
paralelně přes worktree — kalendářní čas viz doporučení pod tabulkou.

| Fáze | Rozsah | Odhad | Náročnost | Hlavní zdroj náročnosti |
|---|---|---|---|---|
| Fáze K — Kontrakt | S | 2–4 dny | **Nízká–střední** | Malý po řádcích kódu, ale je to rozhodnutí, ne implementace — chyba tady (špatný layout) se promítne do F1 i F5 a je drahá na opravu zpětně |
| F0 — sjednocení providera | M | 6–9 dní | **Vysoká** | Mutuje data existující instalace (cutover); kopíruje ověřený Keboola playbook, takže riziko je *snížené*, ne nulové — golden diff musí pokrýt cílový dialekt a kolizi jmen napříč zdroji (O5 zatím neověřeno). Rozsah navíc TBD (K0.11.3) |
| F1 — Distribuce (fyzická cache) | S | 3–5 dní | **Nízká** | Čistě aditivní — nový render krok + doplnění existující CLAUDE.md sekce, žádná schema migrace, nic existujícího se nepřepisuje |
| F2 — Provider granty | S–M | 4–6 dní | **Nízká–střední** | Nový `ResourceType` nepotřebuje DB migraci (CLAUDE.md to garantuje), ale `_can_read_model` třetí větev musí projít RBAC-negativním testem (K0.10) — chyba tady je přesně tvar chyby, co bolí nejvíc |
| F3 — Detach & override | M | 7–10 dní | **Vysoká** | Nejvíc pohyblivých částí v celém plánu: schema migrace (DuckDB+PG pár + kontraktní test dle dual-backend disciplíny), danger-flow UX na dvou místech (detach i re-attach), guard, co dnes vrací flat 409 na více místech najednou |
| F4.1 — Completeness check (cross-doménový) | M | 5–8 dní | **Střední** | Rozšířeno V0 ticketem (K0.11.1) z jednoho enginu na agregaci přes několik subsystémů (semantika, metriky, marketplace, agent profily, corporate memory, glosář) — víc integračních bodů, ne víc logiky na bod |
| F4.2 — Health check | S–M | 3–4 dny | **Nízká–střední** | Agregace nad existujícími sloupci, ale výsledek je neúplný, dokud nedoběhne F3 (odpojené-a-uteklo) |
| F4.3 — Mute s podpisem | S | 2 dny | **Nízká** | Malý audit-trail přírůstek, žádná nová entita |
| F4.4 — Chování agenta + eval harness | S–M | 3–5 dní | **Nízká–střední** | Textová úprava CLAUDE.md je triviální; rozšířeno V0 ticketem o eval set v CI (golden otázky, baseline vs. se sémantikou, práh `<DOPLNIT>`) — harness samotný je ta práce navíc |
| F4.5 — Feedback | S–M | 3–4 dny | **Nízká–střední** | Nová tabulka `semantic_feedback` → schema migrace DuckDB+PG, ale malý, izolovaný povrch |
| F5 — Auto-generace (auto-spuštěný chat agent) | S | 3–4 dny | **Nízká–střední** | Mechanismus rozhodnut (K0.11), takže riziko nižší než v předchozím odhadu — hlavní práce je trigger + notifikace, ne nový generátor |

**Součet:** ~39–56 člověko-dní sekvenčně (o Fázi K a rozšířené F4.1/4.4 víc
než v předchozím odhadu; F4 je pořád pět nezávislých kousků, ne jeden).
Doporučené paralelní pořadí z §4 (Fáze K + F0 + F4.1 + F4.4 souběžně → F1 →
F2+F3 → F4.2-4.5 → F5) stlačuje kalendářní čas na zhruba polovinu při dvou
souběžně pracujících inženýrech (přes `.worktrees/`), protože nejnáročnější
kousky (F0, F3) neleží na stejné závislostní větvi.

---

## 4. Pořadí a závislosti

```
Fáze K (Kontrakt)    — hned, blokuje F1 i F5, neblokuje F0 (jiná vrstva, K0.11.3)
F0 (sjednocení)      — hned, souběžně s Fází K, scope čeká na vstup (K0.11.3)
F1 (Distribuce)      — po Fázi K (render potřebuje zamrzlý layout), nezávislá na F0
F2 (Provider granty) — po F1 (render dědí brány), malá
F3 (Detach)          — nezávislá na F1/F2, kdykoli po F0
F4.1 (Completeness)  — nezávislá, může jít hned (cross-doménová agregace existujícího)
F4.2-4.5             — po F1 (staleness) a F3 (odpojené-a-uteklo)
F5                   — po F4.1 (trigger); mechanismus rozhodnut (K0.11), nezávislý na scaffoldu
```

Závislostní graf se oproti minulé verzi mění na jednom místě: F1 teď visí na
Fázi K (potřebuje zamrzlý layout, ne si ho vymýšlet za pochodu), zatímco
dřív visela jen na sobě. K0.10 dál mění, čemu dát přednost při stejné
velikosti sousta: **F1 a F4.4 táhnout co nejdřív po svých závislostech**,
protože nesou nepoměrně víc rizika/hodnoty než jejich velikost napovídá —
jsou to vlastnosti, které se nedají nahradit lepším promptem. F2/F3 zůstávají
ve stejném pořadí, ale jejich test před vyhlášením "hotovo" by měl zahrnovat
záměrný pokus o únik (grant skupině bez přístupu, ověřit 100% odmítnutí),
ne jen šťastnou cestu.

Doporučení: **Fáze K + F0 + F4.1 paralelně** (tři nezávislé věci — kontrakt,
úložiště, completeness agregace), F4.4 vytažené do stejné vlny i přes
formální závislost na existující CLAUDE.md sekci (K0.2), ne na F1. Jakmile
Fáze K zamrzne (2–4 dny), naskočí **F1**. Pak F2 + F3 (s RBAC-negativním
testem výše) → F4.2-4.5 → F5. Wow efekt vzniká kombinací F1 (agent má
slovník fyzicky) + F4.1 (vidí mezery) + F5 (mezera vede k automaticky
založenému draftu, ne k nové zápisové cestě); důvěryhodnost navenek vzniká
kombinací F1+F4.4 (grounding) + F2/F3 bez úniku (governance).

---

## 5. Otevřené otázky

1. Default TTL cache — **zúženo K0.11.3**: časová TTL zůstává jen jako
   fallback instrukce pro dlouho běžící session (viz F1 bod 3), ne jako
   hlavní invalidační mechanismus. Otevřené: jaký práh pro tenhle fallback
   (kolik hodin session bez pullu je "moc dlouho")?
2. Kdo smí ztlumit health kontrolu — admin only, nebo vlastník zdroje? —
   beze změny.
3. Kde přesně se hlásí špatná odpověď (MCP tool jasný; UI chatů klienta?) —
   beze změny.
4. Scaffold (`data_semantics_scaffold.py`) — zrušit, nebo zúžit na
   bulk-offline use-case? **Odděleno od F5 (K0.11)** — auto-generace ve V0
   jede přes auto-spuštěný chat agent, scaffold už není na kritické cestě.
   Zůstává otevřené, ale bez termínu.
5. Analogie ke K0.3 z Keboola sequencingu: závisí nějaká živá instalace na
   dnešním flat `metric_definitions.id` tvaru pod tím providerem, co F0
   nakonec sjednotí? Ověřit před cutoverem, ať dopadne scope kamkoli.
6. Práh pokrytí, od kterého "svítí" health check — beze změny, 100% nebude
   realistické.
7. Query logy pro budoucí bulk-offline scaffold cestu (pokud k ní dojde) —
   beze změny, nezjištěno.
8. Má se stavět odvození N sub-sémantických vrstev z jednoho providera
   (nezávisle syncovaných a grantovatelných podmnožin) jako řešení K0.8/F2
   limitace? **Blokující termín**: rozhodnutí padá mezi "grant je
   all-or-nothing" a "jde derivovat sub-vrstvy" — jsou to dva popisy jednoho
   sporu, ne dvě nezávislé featury, protože derivovaná sub-vrstva JE
   odebírání. Vlastník rozhodnutí je mimo tenhle dokument, termín
   `<DOPLNIT: DATUM>` — bez rozhodnutí do té doby ho udělá implementace
   (tj. spadne k all-or-nothing default, protože je hotový dřív).
9. **Vyřešeno K0.11.1:** multi-doménový completeness check (dřív "K0.9,
   mimo scope") je jádro V0 — viz aktualizovaná Fáze 4.1. Zbývá jen
   podotázka: procentuální skóre + gamifikace (V1) — navrhnout endpoint z
   4.1 tak, aby šel bez přepisu rozšířit, až přijde na řadu.
10. **Nová (V0 ticket, §7):** kde přesně (repo/cesta) žije verzované schéma
    YAMLů z Fáze K? `<DOPLNIT: repo>` v ticketu — potřebuje rozhodnutí před
    tím, než Fáze K může být "zamrzlá".
11. **Nová (V0 ticket, §7):** acceptance práh pro eval set (F4.4) —
    `<DOPLNIT: X% → Y%>` v ticketu, čeká na vstup od týmu/zákazníka, který
    zadal otázky.

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

---

## 7. V0 ticket (2026-08-25) — mapování na fáze

Nezávisle na tomhle dokumentu vznikl konkrétní, timeboxovaný V0 ticket pro
tuhle práci. Posouvá sémantickou vrstvu na "další level" ve třech osách —
automatizace (Agnes ji umí dodělat sama), preciznost (agent ji čte
spolehlivě), UX (uživatel vidí, co chybí a jak to spravit) — a rozhoduje pár
věcí, které tenhle plán měl jinak (K0.11). Mapování ticketu na fáze výše:

| Bod ticketu | Fáze | Poznámka |
|---|---|---|
| Kontrakt pro agenta (adresářová struktura, YAML schéma, progressive disclosure, MCP/API jako sekundární) | **Fáze K** (nová) | Dělá se první, blokuje F1 i F5 — viz K0.11.2 |
| Semantic adaptéry — co chybí, `unmapped[]` pro lossy mapping | **F0** | Scope čeká na vstup od týmu (K0.11.3), `unmapped[]` doplněno do DoD |
| Auto-generace z dat (native režim), tabulky ve V0 | **F5** | Mechanismus rozhodnut: auto-spuštěný chat-authoring agent, ne deterministický scaffold (K0.11.2, viz aktualizovaná Fáze 5) |
| Completeness check napříč sémantikou/metrikami/skillem/agentem/knowledge base/glosářem, seznam s dopadem, bez % a gamifikace | **F4.1** (rozšířeno) | Dřív odhadnuto jako "mimo scope" (K0.9) — ticket to staví jako jádro V0 (K0.11.1) |
| Evaluace kvality — eval set, baseline vs. se sémantikou, CI | **F4.4** (rozšířeno) | Zároveň test čitelnosti kontraktu z Fáze K |
| Non-goals: uni-directional, no Keboola two-way, sub-layers → otevřené rozhodnutí, uživatelský reporting → V1, % skóre + gamifikace → V1 | principy 2/3, otevřená otázka 8, F4.5, otevřená otázka 9 | Beze změny — ticket potvrzuje směr, který tenhle plán už měl |

**Otevřené vstupy z ticketu, které tenhle dokument nemůže sám doplnit:**
- Kde (repo/cesta) žije verzované YAML schéma z Fáze K — otevřená otázka 10.
- Acceptance práh eval setu (F4.4) — otevřená otázka 11.
- Termín rozhodnutí o sdílení sémantiky mezi skupinami (otevřená otázka 8) —
  ticket říká výslovně: bez rozhodnutí do termínu ho udělá implementace
  (spadne k all-or-nothing default).
- Přesný scope adaptérové práce (F0) — čeká na vstup od týmu (K0.11.3).

**Definition of done (ticket, beze změny):**
1. Kontrakt (layout + schéma) zdokumentovaný a zamrzlý pro V0.
2. Agent čte sémantiku z file systému a použije ji v odpovědi.
3. Native režim: Agnes vygeneruje sémantiku k datasetu bez existující vrstvy.
4. Completeness check běží nad všemi datovými zdroji dané instalace.
5. Eval set v CI, čísla před/po zaznamenaná v ticketu.
6. Zákazník (pro kterého se V0 dělá) to viděl a potvrdil, že to řeší jeho
   problém.
