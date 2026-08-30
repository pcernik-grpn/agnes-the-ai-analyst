# Agnes Multi-Customer Deployment Log

**Datum:** 2026-04-21
**Spec:** `docs/superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`
**Plan:** `docs/superpowers/plans/2026-04-21-multi-customer-deployment.md`

Průběžný log všeho, co bylo uděláno, včetně zvolených hodnot, úprav plánu, objevených překážek a jejich řešení. Cílem je, aby **další zákazník šel nasadit jedním skriptem**.

---

## Přehled

Startup stav: Keboola prod/dev Agnes běžel z osobního forku `padak/tmp_oss` (branch `feature/v2-fastapi-duckdb-docker-cli`), git pull při boot, tokeny v plaintextu v VM metadata. Cíl: přejít na self-deploy model — public upstream `keboola/agnes-the-ai-analyst` + privátní `keboola/agnes-infra-keboola` s Terraformem, GHCR `:stable` image, Secret Manager.

## Konvence

- **Public repo:** `keboola/agnes-the-ai-analyst` (app + TF modul)
- **Privátní repo:** `keboola/agnes-infra-{customer}` (pro Keboolu `keboola/agnes-infra-keboola`)
- **GCP projekt:** `<gcp-project>` (Keboola) — pozn.: ponechán, owner `owner@example.com`
- **Deploy SA:** `agnes-deploy@<project>.iam.gserviceaccount.com`
- **TF state bucket:** `gs://agnes-<project>-tfstate/<customer>/`
- **VM SA:** `agnes-<customer>-vm@<project>.iam.gserviceaccount.com` (scope: secretmanager.secretAccessor)
- **Secrets v SM:**
  - `keboola-storage-token` — sdílený, manuálně vytvořený
  - `agnes-<customer>-jwt-secret` — per-customer, auto-generovaný TF
- **Image tag:**
  - `:stable` (floating) — prod default
  - `:dev` (floating) — dev default
  - `:dev-<branch-slug>` — per-branch (vyžaduje workflow commit — viz Známá omezení)

## Chronologie

### 2026-04-21 odpoledne — Fáze 0 + 1 (MVP)

1. **Ověření IAM přes operativu:** `gcloud iam service-accounts create test...` — funguje i bez přímé role na projektu. Keboola má org-level inherited perms. Owner zůstává `owner@example.com`.
2. **GHCR image public:** `docker manifest inspect ghcr.io/keboola/agnes-the-ai-analyst:stable` funguje bez auth.
3. **Snapshot boot disku:** `data-analyst-pre-migration-20260421` (safety net před Fází 2).
4. **Per-branch tagging v release.yml:** commit `0ade45c` — přidává `:dev-<slug>` tag. **Nepushnuto** do origin kvůli chybějícímu `workflow` scope; uložen jako patch `~/.agnes-keys/0ade45c-workflow-per-branch-tag.patch`.
5. **bootstrap-gcp.sh:** Vytváří SA + role + tfstate bucket + SA key. Spuštěno na `<gcp-project>`. Vytvořen `agnes-deploy` SA, bucket `gs://agnes-<gcp-project>-tfstate`, klíč uložen do `~/.agnes-keys/agnes-deploy-<gcp-project>-key.json`.
6. **Secret Manager:** `keboola-storage-token`, `jwt-secret-key` nahrány (obě s PŘEDCHOZÍMI hodnotami — `jwt-secret-key` aby existing JWT tokeny zůstaly validní; `keboola-storage-token` pro kontinuitu syncu). Rotace tokenu odložena do Fáze 2 completion.
7. **fetch-env-from-secrets.sh:** VM-side skript, který stahuje secrets a skládá `.env`.
8. **Deploy MVP na staré VM `data-analyst`:** 
   - `docker compose down` → `git remote set-url origin https://github.com/keboola/agnes-the-ai-analyst.git` → `git fetch + reset --hard origin/main` → scp fetch-env.sh → `fetch-env.sh` → `docker compose pull + up -d`
   - Ověřeno: `/api/health` `status: degraded` (stale tables, OK), image `ghcr.io/keboola/agnes-the-ai-analyst:stable`, login `admin@example.com / <password>` funguje.
9. **Deploy MVP na staré VM `data-analyst-dev`:** App dir je `/opt/data-analyst/` pod userem `zdeneksrotyr` (jiná struktura než prod). Scope VM je omezený — `fetch-env.sh` selhal, ale .env zůstal beze změny (stejné hodnoty), app běží na `:stable`.
10. **tmp_oss smazán:** Starý osobní fork už neexistoval.

### 2026-04-21 odpoledne — Fáze 2 (TF modul + nové VMs)

11. **TF modul `infra/modules/customer-instance/`:** Refactor z monolitního `infra/main.tf` na reusable modul s:
    - `prod_instance` object + `dev_instances` list (podporuje per-branch image_tag)
    - Persistent `/data` disk (pd-ssd, default 50 GB prod / 20 GB dev)
    - Dedikovaný VM SA `agnes-<customer>-vm` jen s `secretmanager.secretAccessor`
    - Auto-generovaný JWT secret v SM
    - OS Login (`enable-oslogin=TRUE`)
    - Startup script: mount disku, download docker-compose z main branch, fetch secrets, `docker compose up`, volitelně watchtower + Caddy profile
    - **Commit:** `a2c05a5 infra: refactor Terraform into reusable customer-instance module`
12. **Tag `infra-v1.0.0`** push do origin.
13. **Privátní repo `keboola/agnes-infra-keboola`:** Vytvořen v Keboola org. Struktura:
    - `terraform/main.tf` — module reference `github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.0.0`, backend `gcs`
    - `terraform/variables.tf` — default hodnoty pro Keboolu (project, region, prod_instance, dev_instances)
    - `.github/workflows/plan.yml` — PR: `terraform plan` → komentář v PR přes `gh pr comment` (ne `actions/github-script` kvůli validátoru)
    - `.github/workflows/apply.yml` — push main: apply-dev (env `dev`, no protection) → apply-prod (env `prod`, protected_branches, 5min wait, smoke test)
    - GitHub secret `GCP_SA_KEY` nahrán z `~/.agnes-keys/agnes-deploy-*.json`
    - Environmenty `dev` a `prod` vytvořeny přes `gh api`
14. **Terraform apply Keboola instance:** 12 resources vytvořeno:
    - `agnes-prod` VM + `agnes-prod-data` disk (50 GB) + `agnes-prod-ip` (<prod-vm-ip>)
    - `agnes-dev` VM + `agnes-dev-data` disk (20 GB) + `agnes-dev-ip` (<dev-vm-ip>)
    - Firewall `agnes-keboola-allow-web`
    - `agnes-keboola-vm` SA + IAM binding
    - `agnes-keboola-jwt-secret` + version
    - TF state v `gs://agnes-<gcp-project>-tfstate/keboola/`
15. **Data migration starý prod → nový prod (~2 min):**
    - `docker compose down` na starém prod VM
    - `tar czf /tmp/agnes-data.tar.gz -C /var/lib/docker/volumes/app_data/_data .` (1.8 GB)
    - `gsutil cp` do `gs://agnes-<gcp-project>-tfstate/migration/agnes-data-20260421-1624.tar.gz`
    - **Problém:** `agnes-keboola-vm` SA neměl `storage.objectViewer` na bucketu → `gsutil iam ch serviceAccount:...:objectViewer gs://...` (dočasné, pro download)
    - `docker compose down` na novém prod VM
    - `gsutil cp` z bucketu na nový VM + `tar xzf ... -C /data`
    - `docker compose up -d` na novém prod VM
    - **POZOR:** Analytics DB se nezbudovala automaticky po extrakci — viz Známá omezení.

## Klíčové hodnoty (kopíruj pro další zákazníky)

```
GCP_PROJECT_ID        = <gcp-project>
CUSTOMER_NAME         = keboola
DEPLOY_SA             = agnes-deploy@<gcp-project>.iam.gserviceaccount.com
TFSTATE_BUCKET        = gs://agnes-<gcp-project>-tfstate
TFSTATE_PREFIX        = keboola
VM_SA                 = agnes-keboola-vm@<gcp-project>.iam.gserviceaccount.com
JWT_SECRET            = agnes-keboola-jwt-secret (TF-managed)
KEBOOLA_TOKEN_SECRET  = keboola-storage-token (manuálně vytvořený)
INFRA_MODULE_REF      = infra-v1.0.0 (github.com/keboola/agnes-the-ai-analyst)
PROD_IP               = <prod-vm-ip> (agnes-prod)
DEV_IP                = <dev-vm-ip> (agnes-dev)
STARÝ PROD IP (legacy) = <redacted-ip> (data-analyst — po stabilitě smazat)
STARÝ DEV IP (legacy)  = <redacted-ip> (data-analyst-dev — po stabilitě smazat)
```

## Známá omezení / TODO

### Workflow commit nepushnutý
Commit `0ade45c` (per-branch `:dev-<slug>` tag v release.yml) vyžaduje `workflow` scope na GH tokenu, který aktuální token nemá. Uloženo v `~/.agnes-keys/0ade45c-workflow-per-branch-tag.patch`.

**Akce pro dokončení:**
```bash
gh auth refresh -h github.com -s workflow
cd <public-repo>
git am ~/.agnes-keys/0ade45c-workflow-per-branch-tag.patch
git push origin feature/multi-customer-deployment
```

Bez toho fungují jen floating tagy `:dev` a `:stable`, ale ne pinned `:dev-<branch-slug>` v `dev_instances`.

### Analytics DB se po migraci dat nepřebudovala
Po kopii `/data` přes tar na nový prod VM má `system.duckdb` všechno (table_registry, users), ale analytics DB je prázdná — SyncOrchestrator nespustil `rebuild()` automaticky. Endpoint `/api/sync/trigger` nebo `/api/sync/rebuild` bude třeba dohledat v app API a zavolat autentizovaně.

### Dev VM `data-analyst-dev` staré scope
Staré `data-analyst-dev` má omezené compute SA scope bez Secret Manageru. V Fázi 2 se nahrazuje novým `agnes-dev` (s dedikovaným VM SA), staré zruš po ověření stability.

### Starý Keboola token nerotován
Nový token v SM je stále ten stejný, co byl v `.env` na starém VM. Po ověření stability nového proudu v Keboola UI vygenerovat nový + `gcloud secrets versions add keboola-storage-token` + restart containerů. Starý pak invalidovat.

### Admin heslo `<password>` na starém prod
Migrace dat zkopírovala users table, takže heslo je platné i na novém prod. Rotace je uživatelův úkon přes UI. Nové dev VM má jiný state → jiné hesla.

## Co zbývá (uživatelské akce)

- [ ] **Approve prod environment** v `apply.yml` runu (https://github.com/keboola/agnes-infra-keboola/actions/runs/24731681502) — jinak se state neaplikuje na prod
- [ ] **Změnit heslo admin usera** z `<password>` (http://<prod-vm-ip>:8000/login → profil)
- [ ] **Rotovat Keboola Storage token** v Keboola UI → `gcloud secrets versions add keboola-storage-token --data-file=- --project=<gcp-project>` → restart app containerů na obou VMs (cron to zachytí při dalším tiku nebo `sudo /usr/local/bin/agnes-auto-upgrade.sh`)

## Aktualizace průběhu (2026-04-21 pozdně)

### Fixy po první migraci

1. **Docker named volume → bind mount /data:**
   Po první migraci nové VMs používaly `agnes_data` Docker named volume (uložený na boot disku 30GB), nikoli persistent disk mountovaný na `/data` (50GB). Fix: v `docker-compose.prod.yml` override volume `data` jako bind mount `/data`. Commit `52d6345`. Bumplý tag `infra-v1.1.0`.

2. **Watchtower → cron:**
   `containrrr/watchtower` (v1.7.1 i latest) má nekompatibilní Docker API (posílá 1.25, daemon vyžaduje 1.40+). Nahrazen bash skriptem `/usr/local/bin/agnes-auto-upgrade.sh` spouštěným cronem každých 5 min. Detekuje změnu image digest, pokud ano, pullne + `docker compose up -d`. Commit `cbd85c5` v modulu, tag `infra-v1.1.0`.

3. **Ověření auto-upgrade:**
   Během finálního verify cyklu cron pullnul novější `:stable-2026.04.33` (nejnovější release) a recreate containers na prod. Fungování potvrzené.

### Iterace 2 — finalizace (2026-04-21 večer)

1. **Workflow commit pushnut** — po `gh auth refresh -s workflow` protlačen `0ade45c` + merge do main. Per-branch tagging `:dev-<slug>` v GHCR aktivní.
2. **Dev data zmigrovaná** — `data-analyst-dev` → lokál → `agnes-dev`. DuckDB registry obsahuje 99 tabulek + 1 admin usera.
3. **Module bumpnut na v1.2.0 v Keboola infra repu** — README plně v EN, CI spustí čistý plan.
4. **Backup + monitoring → infra-v1.3.0:** daily snapshot schedule na `/data` disku (30d retention), per-VM uptime check + alert policy. Template repo bumpnut na v1.3.0.
5. **Renovate config** v template + keboola-infra repu — tracks `infra-v*` tagy, otevírá PR při nové verzi.
6. **Staré VMs smazané** — `data-analyst`, `data-analyst-dev`, jejich static IP, pre-migration snapshot, migration tar z bucketu.
7. **Temporary IAM grants revokovány** — `secretmanager.secretAccessor` odebrán z default compute SA (na secrets), `storage.objectViewer` odebrán z `agnes-keboola-vm` na tfstate bucket.
8. **Onboarding ONBOARDING.md rozšířen** o propagation přes `-replace`, backup restore, monitoring setup, race condition fix.
9. **Auth v2 → v3 action bump** v obou workflow repech (silences Node 20 deprecation warning).
10. **Prod apply-dev úspěšně proběhl** po manuálním triggeru (initial apply měl race s timing secret creation). apply-prod čeká na reviewera.

### Iterace 4 — version badge + workflow-driven recreate

1. **Version badge v UI**: `/api/version` endpoint + footer badge v `base.html` loadí asynchronně, zobrazuje `<channel>-<version> · <tag> · deployed <relative> (<UTC>)` s commit SHA v tooltipu.
2. **Module `infra-v1.5.0`**: startup-script odvozuje `AGNES_VERSION` a `RELEASE_CHANNEL` z image tagu (stable-YYYY.MM.N / dev-…) a zjistí `AGNES_COMMIT_SHA` z `docker pull` digest. Tyto vars jdou do `.env` → app čte → `/api/version` vrací → badge renderuje.
3. **`workflow_dispatch` s `recreate_targets`** v `apply.yml` (oba repa): manuálně spustitelný workflow input s comma-separated TF resource addresses → `-replace=<addr>` předáno `terraform apply`. Řeší `ignore_changes = [metadata_startup_script]` gotchu. Dev targets routed do `apply-dev`, prod do `apply-prod`.
4. **Dokumentace propagation přepsána** v `docs/ONBOARDING.md` — Option A (workflow_dispatch, recommended) vs Option B (local TF), plus explicit DO NOT sekce proti ručnímu SSH zásahu.

### Iterace 3 — code review + bootstrap fix + doc sweep

1. **Code review** dispatched přes `superpowers:requesting-code-review` subagent. Nálezy: 7 critical, 9 important, 10 minor.
2. **Critical + important fixy → `infra-v1.4.0`:**
   - C1 VM SA scoped per-secret (ne project-wide)
   - C3 `chmod 640` na startup log
   - C4 Fail-fast když `keboola-storage-token` chybí (odstraněn `|| echo ""`)
   - C5 Cron auto-upgrade sources `.env` pro `AGNES_TAG`
   - C7 `depends_on` na IAM bindings + secret version (eliminuje první-boot race)
   - I1 Firewall split: `:8000` conditional na tls_mode; SSH v samostatné rule
   - I2 `firewall_ssh_source_ranges` var (default: IAP tunnel range)
   - I4 `compose_ref` var pinuje docker-compose files
   - I5 `acme_email` var (falls back to seed_admin_email)
   - I6 Merge order v `dev_instances` (user values win over defaults)
   - I7 `|| true` na Caddyfile fetch odstraněno
3. **A test — `/auth/bootstrap` bug fix:** SEED_ADMIN_EMAIL seeded usera bez hesla, který blokoval bootstrap endpoint. Fix: endpoint je teď disabled jen když existuje user s `password_hash`. Seedované passwordless users může endpoint activate (set password + promote to admin). Tests: 8/8 passed.
4. **B test — dry-run onboarding:** našel 2 gapy v templatu (module pinoval v1.3.0, tfvars.example měl CZ komentáře). Bumpnuto na v1.4.0, komentáře přeloženy, přidány docs pro nové vars.
5. **Wait timer** na prod GitHub environment v keboola-infra repu odebrán (0 s) — reviewer-only gate.
6. **Node 20 deprecation warning** — `google-github-actions/auth@v2 → v3`; pro `hashicorp/setup-terraform@v3` (stále Node 20) přidán `FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true` env-var, který force Node 24 runtime.
7. **Docs sweep:**
   - `docs/DEPLOYMENT.md` přepsán — rozcestník Terraform (recommended) vs Docker Compose (OSS self-host)
   - `docs/ONBOARDING.md` sekce 4 + 6 aktualizované pro v1.4.0 (nové vars, bootstrap semantics)
   - `README.md` docs list expanded
   - `keboola/agnes-infra-keboola` README bumped + wait-timer note

### Finální stav (po iteraci 3)

| Resource | Value |
|---|---|
| **Prod VM** | `agnes-prod` @ <prod-vm-ip> (e2-small, 50GB /data PD, daily snapshot, uptime check) |
| **Dev VM** | `agnes-dev` @ <dev-vm-ip> (e2-small, 20GB /data PD, daily snapshot, uptime check) |
| **Staré VMs** | 🗑️ smazané |
| **Image tagy** | prod `:stable`, dev `:dev`, feature branches `:dev-<slug>` (aktivní po v1.4) |
| **Auto-upgrade** | Cron `*/5 * * * *` — reads AGNES_TAG z .env, digest change → restart |
| **Prod health** | `degraded` (stale tables), 103 tables, 9.3M rows, 2 users |
| **Dev DB** | 99 tables v registry, admin user `admin@example.com` |
| **Backups** | Daily snapshot @ 02:00, 30-day retention (oba data disky) |
| **Monitoring** | uptime check 60s/10s per VM, alert > 5 min failure (notification channels nenapojené) |
| **Firewall** | Web 80/443 + 8000 (jen když TLS off); SSH na IAP range only |
| **Login prod** | `admin@example.com` / `<password>` *(pending: user rotate)* |
| **Login dev** | `admin@example.com` / `<password>` *(pending: user rotate)* |
| **TF state** | `gs://agnes-<gcp-project>-tfstate/keboola/` (versioned, GCS backend) |
| **Deploy SA** | `agnes-deploy@<gcp-project>.iam.gserviceaccount.com` |
| **VM SA** (scope: secretmanager.secretAccessor per-secret) | `agnes-keboola-vm@<gcp-project>.iam.gserviceaccount.com` |
| **Secrets** | `keboola-storage-token` (manual), `agnes-keboola-jwt-secret` (TF), `jwt-secret-key` (legacy) |
| **Public upstream repo** | https://github.com/keboola/agnes-the-ai-analyst |
| **Template repo** | https://github.com/keboola/agnes-infra-template (is_template=true, ref infra-v1.4.0) |
| **Keboola infra repo** | https://github.com/keboola/agnes-infra-keboola (EN README, Renovate, ref **infra-v1.4.0**) |
| **Module tagy** | `v1.0.0` → `v1.1.0` (volume+cron) → `v1.2.0` (CI fix) → `v1.3.0` (backups+monitoring) → **`v1.4.0` (review fixes)** |

### Onboarding druhého zákazníka — kompletní flow

Podle [`docs/ONBOARDING.md`](../../ONBOARDING.md) — cíl: < 1 hodina. Klíčové kroky:

1. `bootstrap-gcp.sh <PROJECT_ID>` — SA + bucket + klíč
2. `gcloud secrets create keboola-storage-token ...` (pokud source = keboola)
3. `gh repo create <org>/agnes-infra-<cust> --template keboola/agnes-infra-template --private`
4. Upload GCP_SA_KEY do GH secret
5. Editovat `terraform/main.tf` (backend bucket/prefix) + `terraform.tfvars`
6. Vytvořit `dev` + `prod` environments přes `gh api`
7. `git push` → CI apply
8. `POST /auth/bootstrap` admin user
9. Otestovat `/api/health` + login

Předpokládám, že nový zákazník (např. another-customer) projde všech 9 kroků za **~30–45 min** včetně čekání na TF apply.


## Budoucí one-click deploy

Cíl: pro nového zákazníka `{customer}` (např. `another-customer`) by mělo stačit:

```bash
# 1. Vytvořit GCP projekt (má billing)
gcloud projects create agnes-{customer}

# 2. Bootstrap GCP (SA + bucket + role + klíč)
./scripts/bootstrap-gcp.sh agnes-{customer}

# 3. Vytvořit Keboola Storage secret v zákaznickém SM (manuálně, token dodá zákazník)
echo -n "<KEBOOLA_TOKEN>" | gcloud secrets create keboola-storage-token \
    --data-file=- --replication-policy=automatic --project=agnes-{customer}

# 4. Klonovat template repo (template repo musí existovat — Fáze 6)
gh repo create {org}/agnes-infra-{customer} --template keboola/agnes-infra-template --private

# 5. Upload SA key do GH secretu
cd agnes-infra-{customer}
gh secret set GCP_SA_KEY < ~/.agnes-keys/agnes-deploy-agnes-{customer}-key.json

# 6. Vyplnit terraform/terraform.tfvars (customer_name, project, IP preferences)

# 7. První apply — spustí CI/CD a nahodí VMs
git add . && git commit -m "initial" && git push
```

Co tomu ještě chybí:
- Template repo (Fáze 6)
- Onboarding skript, který provede kroky 1–7 interaktivně
- Dokumentace: jak nastavit DNS, TLS, admin account bootstrap
