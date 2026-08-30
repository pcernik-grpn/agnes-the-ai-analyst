# Multi-Customer Deployment — Design Spec

Datum: 2026-04-21
Status: Návrh k implementaci
Autor: Zdeněk Šrotýř + Claude (sparring)

## 1. Cíl

Zavést *production-grade* nasazení Agnes, které:

1. Nechává **upstream repo public** (žádné zákaznické info tam).
2. Umožňuje **N zákazníků paralelně**, každý v izolovaném prostoru.
3. Je **anonymizované** — jeden zákazník nevidí existenci ani identitu ostatních.
4. Má **auto-deploy s rozumnými gates** — feature branch push → dev VM aktualizace do minut; merge do main → prod s review gate.
5. Podporuje **branch-aware dev environments** — víc vývojářů paralelně, každý na své branchi, bez interference.
6. **Škáluje O(1) na zákazníka** — přidání another-customer vedle Keboola znamená jen klonování šablony, ne změnu upstream.

## 2. Model — Pure Self-Deploy

### 2.1 Role

| Strana | Co dělá |
|---|---|
| **Keboola jako upstream** | Udržuje app kód, buildí & pushuje Docker image na GHCR, udržuje TF modul, udržuje infra template |
| **Zákazník (vč. Keboola-as-customer)** | Vlastní GCP projekt, vlastní privátní infra repo, vlastní CI/CD, spravuje svoje VMs, nese náklady |

Keboola jako upstream **nemá žádný přístup k zákaznickým GCP projektům**. Zákazník zodpovídá za svoje nasazení.

Keboola interní produkční Agnes instance je **speciální případ zákazníka** — Keboola IT vlastní `<gcp-project>` GCP projekt a spravuje tam svou Agnes stejně jako to bude dělat another-customer ve svém GCP.

### 2.2 Budoucí rozšíření (out of scope pro tuto vlnu)

- **AWS podpora**: TF modul je dnes GCP-specific. Jakmile přijde první AWS zákazník, přidáme paralelní modul `modules/customer-instance-aws/`.
- **Managed service**: Keboola bude nabízet "nasadíme vám to za vás" — znamená přidat Keboola jako operator role s IAM delegací do zákazníkova GCP. Design v tomhle specu je kompatibilní, jen vyžaduje extra vrstvu IAM bindings.

## 3. Repo architektura

### 3.1 Počet a typ repozitářů

```
keboola/agnes-the-ai-analyst        PUBLIC       App + TF modul + dokumentace
keboola/agnes-infra-template        PUBLIC       Skeleton pro privátní infra repo (template)
keboola/agnes-infra-keboola         PRIVATE      Keboola-as-customer deployment
{acme}/agnes-infra                  PRIVATE      Nový zákazník — v jejich GitHub org, klonováno z template
```

Počet: **2 upstream + N per-customer**. Upstream repa jsou stabilní, per-customer vznikají při onboarding.

### 3.2 Obsah `keboola/agnes-the-ai-analyst` (public)

```
agnes-the-ai-analyst/
├── app/ src/ connectors/ cli/        # produkt
├── Dockerfile docker-compose.yml
├── .github/workflows/
│   └── release.yml                   # build + push do GHCR; tagy: :dev, :stable, :dev-branch-xyz
├── infra/
│   ├── modules/
│   │   └── customer-instance/        # versioned: tag infra-v1.0, v1.1, ...
│   │       ├── main.tf
│   │       ├── variables.tf
│   │       └── outputs.tf
│   └── examples/
│       └── minimal/                  # quickstart pro OSS self-hoster
└── docs/
    ├── DEPLOYMENT.md                 # pro self-host (compose, bez Terraform)
    ├── ONBOARDING.md                 # pro managed (cesta k TF + template)
    └── architecture.md
```

**TF modul `customer-instance`** je verzován samostatně semver (`infra-v1.x`), odlišeně od app image (CalVer `YYYY.MM.N`).

### 3.3 Obsah `keboola/agnes-infra-template` (public template)

```
agnes-infra-template/
├── terraform/
│   ├── main.tf                       # module { source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.0" }
│   ├── variables.tf
│   ├── backend.tf                    # gcs by default, komentář jak přepnout na s3/remote
│   ├── terraform.tfvars.example
│   └── .gitignore                    # terraform.tfvars, *.tfstate
├── .github/workflows/
│   ├── plan.yml                      # PR → terraform plan
│   └── apply.yml                     # main → terraform apply
├── config/
│   └── instance.yaml.example
├── bootstrap.sh                      # jednorázový setup GCP: SA, API enable, bucket, secrets
└── README.md                         # step-by-step onboarding
```

Zákazník (nebo Keboola při onboardingu) použije `gh repo create --template keboola/agnes-infra-template` → přijde privátní repo s hotovou strukturou.

### 3.4 Obsah per-customer privátního repa (např. `keboola/agnes-infra-keboola`)

Přesně ta samá struktura jako template, jen s konkrétními hodnotami v `terraform.tfvars`:

```hcl
# keboola/agnes-infra-keboola/terraform/terraform.tfvars
# (gitignored, nebo lokálně v Secret Manageru — viz §6)

gcp_project_id  = "<gcp-project>"
region          = "europe-west1"
zone            = "europe-west1-b"

prod_instance = {
  name         = "agnes-prod"
  machine_type = "e2-small"
  image_tag    = "stable"              # floating | "stable-2026.04.N" (pinned)
  upgrade_mode = "auto"                # auto (watchtower) | pinned (Renovate)
  tls_mode     = "caddy"               # caddy | gcp-lb | cloudflare | none
  domain       = ""                    # prázdné = jen IP
}

dev_instances = [
  { name = "agnes-dev-default", image_tag = "dev" },
  # přidávat další dev VMs per branch/developer
]

seed_admin_email = "admin@example.com"

# Keboola-specific
data_source        = "keboola"
keboola_stack_url  = "https://connection.us-east4.gcp.keboola.com/"
keboola_token_secret_id = "keboola-storage-token"   # reference do Secret Manageru
```

## 4. Release model

### 4.1 Image tagging v GHCR

Public repo CI (release.yml) buildí a pushuje do `ghcr.io/keboola/agnes-the-ai-analyst` při každém push:

| Trigger | Tagy které vzniknou |
|---|---|
| Push `main` | `:stable`, `:stable-YYYY.MM.N`, `:sha-xxxxxxx` |
| Push `feature/xyz` | `:dev`, `:dev-feature-xyz`, `:sha-xxxxxxx` |
| Push `release/1.2.x` | `:release-1.2.x`, `:release-1.2.x-YYYY.MM.N` |

`:dev` a `:stable` jsou **floating** tagy — posouvají se při každém pushe. Verzované tagy jsou **neměnné**.

### 4.2 Visibility obrazu

`ghcr.io/keboola/agnes-the-ai-analyst` je **public image**. Zákaznické VMs pullují bez credentials.

Důvod: kód je veřejný, obraz nesmí obsahovat nic, co veřejný kód neobsahuje. Secrets jdou do `.env` na VM, ne do image.

### 4.3 Smoke test

Po push `main` a tagování `:stable-N`, CI spustí smoke test: `docker compose up` + curl `/api/health` + auth + query. PASS → `:stable` floating se posune. FAIL → build dostane `:deprecated-N` label, `:stable` se nehne, GitHub issue s logy.

### 4.4 CalVer + smoke test = kontinuální release

Žádné manuální release rozhodnutí. Každý merge do main = release (pokud smoke test projde). Číslování `YYYY.MM.N` = rok.měsíc.sekvence.

## 5. Branch-aware dev environments

### 5.1 Motivace

Víc vývojářů paralelně potřebuje víc dev environmentů bez interference. „Floating `:dev`" je nedostatečné — poslední push přepíše ostatní.

### 5.2 Mechanismus

Každý feature branch push → samostatný tag `:dev-{branch-slug}` navíc k floating `:dev`.

V privátním infra repu zákazník vyjmenuje dev VMs s pinned tagem:

```hcl
dev_instances = [
  { name = "agnes-dev",          image_tag = "dev" },                         # floating (demo / reviewers)
  { name = "agnes-alice-feat1",  image_tag = "dev-feature-alice-dashboard" }, # Alice má svou
  { name = "agnes-bob-pr142",    image_tag = "dev-pr-142" },                  # Bob pinned na PR
]
```

### 5.3 Lifecycle dev VM

```
1. Někdo otevře PR v privátním infra repu:
   +   { name = "agnes-carol", image_tag = "dev-feature-carol-new-auth" }
2. CI plan.yml komentuje v PR: „vytvoří se VM agnes-carol (e2-small, europe-west1-b)"
3. Merge → apply.yml spustí terraform apply
4. VM up za ~2 min
5. Watchtower na VM polluje :dev-feature-carol-new-auth každých 5 min
6. Každý push na feature/carol/new-auth → nový image → watchtower pullne → VM má aktuální verzi
7. Až Carol dokončí feature (merge do main), smaže řádek v tfvars → terraform apply → VM destroy
```

**Žádný nový SA, žádný nový GitHub environment, žádná infra operace navíc.** Jen editace seznamu v tfvars.

### 5.4 Ephemeral preview environments (budoucnost)

V pozdější fázi zvážit automatizaci: PR otevřen → GHA vytvoří per-PR VM; PR zavřen → destroy. Aktuálně explicitní flow přes tfvars stačí.

## 6. Prod upgrade model

### 6.1 Dva režimy (per-instance volitelné)

| Režim | Jak | Pro koho |
|---|---|---|
| **auto** | Watchtower na VM polluje `:stable` (floating), pullne + restart, když se objeví nový digest | Default — rychlost, low-touch |
| **pinned** | `image_tag = "stable-2026.04.7"` v tfvars. Renovate polluje GHCR, otevírá PR s bump. Ops schválí → merge → apply | Regulovaní zákazníci, audit trail |

### 6.2 Gate pro auto režim

Jedinou ochranou před rozbitým `:stable` je **CI smoke test** před posunutím floating tagu. Pokud projde tam, prod auto-upgradne. Doporučení: mít i u Keboola instance **monitoring + alert na `/api/health` degraded status**, aby případný skluz smoke testu nezůstal dlouho bez povšimnutí.

### 6.3 Rollback

Rollback = změnit `image_tag` na předchozí verzi a `docker compose up -d`. Zjednodušená forma:

- **Auto režim:** rychle přepnout watchtower na specifický tag; pak investigate
- **Pinned režim:** PR revert, apply

## 7. Security model

### 7.1 Authentication mezi komponenty

| Kdo → kde | Jak se přihlásí |
|---|---|
| Public CI → GHCR push | `${{ secrets.GITHUB_TOKEN }}` (built-in) |
| VM → GHCR pull | Public image, bez auth |
| Privátní CI → GCP | SA JSON key v `GCP_SA_KEY` secret (Fáze 1); WIF (Fáze follow-up) |
| CI na zákaznickém GCP → Secret Manager | SA má `roles/secretmanager.admin` |
| App na VM → Secret Manager | VM má dedikovaný SA s `roles/secretmanager.secretAccessor` |
| App na VM → Keboola Storage | Token z Secret Manageru |

### 7.2 Deploy SA — scope per zákazník

SA `agnes-deploy@<gcp-project>` dostane **jen** tyto role:

```
roles/compute.instanceAdmin.v1     # create/update/delete VMs
roles/compute.securityAdmin        # firewall rules
roles/compute.networkAdmin         # static IP
roles/iam.serviceAccountUser       # attach VM SA k instancím
roles/secretmanager.admin          # vytvořit/rotovat secrets
roles/storage.admin                # tfstate bucket
```

Žádný `owner`, žádný `editor`. Blast radius pro leak SA key = přepis VMs v tomhle projektu. Nic mimo projekt, nic dat.

### 7.3 GitHub environmenty

```yaml
environments:
  dev:
    # žádná protection
    secrets:
      GCP_SA_KEY: <same key>
  prod:
    protection_rules:
      required_reviewers: [@keboola-ops-team]
      wait_timer: 5m
      deployment_branches: main
    secrets:
      GCP_SA_KEY: <same key>
```

Oba environmenty sdílí ten samý SA key (jeden GCP, jedna identita). Rozdíl je **jen v protection rules** — kdo smí pushnout kam.

### 7.4 VM hardening

- **OS Login** místo per-user SSH klíčů (follow-up)
- **Dedikovaný VM SA** s minimem práv (jen read z Secret Manageru, nic dalšího)
- **Ephemeral disk strategy**: boot disk = produkt (stateless), `/data` = persistent disk (stateful, snapshoty)
- **Žádný token v startup-script metadatě** — všechny secrets teprve při boot z Secret Manageru

### 7.5 Rotace tajemství

| Tajemství | Kde žije | Jak se rotuje |
|---|---|---|
| Keboola Storage token | Secret Manager v zákaznickém GCP | Keboola UI → nová verze v SM → app restart |
| JWT_SECRET_KEY | Secret Manager, generováno TF | `terraform apply` s `-replace=google_secret_manager_secret_version.jwt` |
| SA JSON key | GitHub secret | Vygenerovat nový klíč, paste do GH secret, smazat starý klíč v GCP |
| User passwords | Argon2 hash v DuckDB `users` | User-facing flow (reset endpoint, admin CLI) |

## 8. Onboarding nového zákazníka

### 8.1 Kroky (cílový čas: < 1 hod)

```
1. Zákazník (nebo Keboola ops za něj) založí GCP projekt + billing
2. Někdo s owner rolí v projektu spustí bootstrap.sh:
   - Enable APIs (compute, iam, secretmanager, storage, iamcredentials)
   - Vytvoří SA agnes-deploy s rolemi
   - Vygeneruje SA key (předá ownerovi)
   - Vytvoří gs://agnes-{project}-tfstate
3. Zákazník (nebo Keboola ops) klonuje template:
   gh repo create {org}/agnes-infra --template keboola/agnes-infra-template --private
4. V novém repu:
   - Nastaví GH secret GCP_SA_KEY (paste z kroku 2)
   - Upraví terraform.tfvars na jejich hodnoty
   - Vytvoří initial commit + push
5. Nastaví Secret Manager tajemství (Keboola token atd.)
6. První PR s tfvars → plan → merge → apply
7. DNS — zákazník si později nastaví CNAME na IP (nebo zůstane na IP)
8. Admin user — bootstrap endpoint POST /auth/bootstrap nebo admin CLI
9. Smoke test: login, sync, query
```

### 8.2 Co je vidět komu

| Role | Vidí |
|---|---|
| Každý na internetu | Public repo `agnes-the-ai-analyst`, jeho issues, PRs, image na GHCR |
| Keboola ops tým | Výše + privátní template repo + infra-keboola repo |
| Zákazník (acme) | Výše public + svůj vlastní infra-acme repo ve svém org |
| Nikdo | Ostatní zákazníky kromě jejich vlastního |

## 9. Tok změn

### 9.1 Change v app kódu (nejčastější)

```
1. Vývojář: push feature branch v public repu
2. Public CI: build :dev-feature-xyz (a :dev floating)
3. Watchtower na každé VM s image_tag = "dev": pullne do 5 min
   Watchtower na VM s image_tag = "dev-feature-xyz": pullne taky
4. Dev review
5. Merge do main
6. Public CI: build :stable-YYYY.MM.N (a :stable floating)
7. Smoke test CI: PASS → :stable se posune
8. Prod VMs:
   - auto režim: watchtower pullne do 5 min
   - pinned režim: Renovate otevře PR v privátním repu
```

### 9.2 Change v infra (VM size, dev VM list, nová disk)

```
1. Ops otevře PR v privátním infra repu
2. CI plan.yml: terraform plan → komentář v PR
3. Review + merge
4. CI apply.yml:
   - pro dev změny: environment "dev" → apply bez gatu
   - pro prod změny: environment "prod" → required reviewer → apply
5. Po apply: smoke test přes curl /api/health
```

### 9.3 Change v TF modulu

```
1. Maintainer otevře PR v public repu do infra/modules/customer-instance/
2. CI validuje modul proti examples/
3. Merge → auto git tag infra-v1.1.0
4. Renovate v každém privátním infra repu:
   → otevře PR "bump source ref to infra-v1.1.0"
5. Každý zákazník schvaluje samostatně → terraform plan → apply
```

## 10. Provozní aspekty

### 10.1 Monitoring a alerting (doporučení, ne v první vlně)

- Cloud Monitoring dashboard per-customer
- Alert na `/api/health` `status != "healthy"` déle než 5 min
- Alert na VM CPU > 80 % déle než 30 min
- Log-based metric: sync failures, auth failures, HTTP 5xx rate
- Integrace se Slack/email přes Alerting policy

### 10.2 Backup

- Snapshoty `/data` persistent disku denně, retention 30 dní (TF `google_compute_resource_policy`)
- `system.duckdb` obsahuje users/permissions — při schema migraci snapshot kopie (již existuje jako `*.pre-migrate`)

### 10.3 Disaster recovery

- Recreation VM z nuly = `terraform apply` (~5 min) + restore `/data` ze snapshotu (~5 min)
- Total loss zákazníka = destroy GCP projektu; recreate ze snapshotu + tfstate

### 10.4 Cost per customer (orientačně)

| Položka | $/měs |
|---|---|
| Prod VM e2-small + 30GB SSD | ~$15 |
| Dev VM e2-small + 30GB SSD | ~$15 |
| Persistent disk (50 GB) | ~$2 |
| Static IP (×2 — prod, dev) | ~$5 |
| Snapshots (daily, 30d retention) | ~$2 |
| Secret Manager | ~$0 (pod freetier) |
| **Celkem base** | **~$40/měs** |

Škáluje lineárně s počtem dev VMs.

## 11. Principy / Non-goals

- ✅ **Public upstream zůstává public.** Nic, co zákazníka identifikuje, tam není.
- ✅ **Zákazník má plnou kontrolu svého nasazení.** Včetně rozhodnutí, zda upgradovat.
- ✅ **Žádná centrální Keboola ops infra.** Žádný sdílený GCP projekt, žádný sdílený state.
- ❌ **Není to multi-tenant** v jednom deploymentu. Jeden `docker compose up` = jeden zákazník.
- ❌ **Keboola není SaaS hostér** (aspoň ne teď). Pokud zákazník chce managed, je to ručně poskytnutá služba, ne produkt.
- ❌ **Žádný cross-customer routing.** Žádný sdílený load balancer, žádný sdílený DNS.

## 12. Rozhodnutí a otázky

Všechny designové otázky, které vznikly během brainstormingu, jsou vyřešené. Odkazy zde pro trasovatelnost:

| Otázka | Rozhodnutí |
|---|---|
| Managed vs self-deploy | A) Pure self-deploy (mění se v Fázi 2+ pokud bude potřeba) |
| Centrální ops repo | Ne — 1 public + 1 template + N per-customer |
| TF state lokace | gs:// v zákaznickém GCP (default); flex na S3/TFC v template |
| Template repo název | `keboola/agnes-infra-template` |
| CI auth | SA JSON key v GH secret (Fáze 1); WIF (follow-up) |
| Image visibility | Public na GHCR |
| Prod upgrade režim | Per-instance volba auto/pinned, default auto |
| TLS | Caddy default, flex na gcp-lb/cloudflare |
| DNS | Zákazník si řeší sám, default jen IP |
| GCP projekt pro Keboola | `<gcp-project>` zůstává |
| Dev VM model | Seznam `dev_instances` v tfvars, per-položka image_tag |
| `ZdenekSrotyr/tmp_oss` | Smazat po Fázi 1 |

## 13. Glosář

| Zkratka | Význam |
|---|---|
| **GHCR** | GitHub Container Registry — ghcr.io |
| **WIF** | Workload Identity Federation — GCP mechanismus auth CI bez static key |
| **SA** | Service Account (GCP) |
| **TF** | Terraform |
| **OIDC** | OpenID Connect — auth protokol, GitHub vydává OIDC tokeny pro GHA |
| **CalVer** | Calendar Versioning — YYYY.MM.N |
| **PD** | Persistent Disk (GCP) |

## 14. Follow-up iterace

Mimo scope této první vlny, ale plánováno:

- **WIF místo SA JSON key** (bezpečnost)
- **OS Login** (odstranění osobních SSH klíčů)
- **Monitoring + alerting** (Cloud Monitoring, Slack integration)
- **Automatické snapshoty** + restore procedura
- **Ephemeral PR preview environments**
- **AWS podpora** (paralelní TF modul)
- **Plugin API** pro proprietární customer extensions (viz issue #8)
- **Managed service varianta** (Keboola hostuje za zákazníka)

## 15. Reference

- Předchozí spec: `docs/superpowers/specs/2026-04-09-multi-instance-deployment-design.md` (CalVer release model)
- Issue: keboola/agnes-the-ai-analyst#8 — plugin API for private customer extensions
