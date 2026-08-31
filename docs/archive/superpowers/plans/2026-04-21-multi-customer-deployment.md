# Multi-Customer Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Přejít z dnešního "prod běží z osobního forku padak/tmp_oss" na production-grade multi-customer setup podle spec `docs/superpowers/specs/2026-04-21-multi-customer-deployment-spec.md`.

**Architecture:** Public upstream (`keboola/agnes-the-ai-analyst`) s TF modulem + public image na GHCR. Privátní template repo (`keboola/agnes-infra-template`) jako skeleton. Per-customer privátní repo (`keboola/agnes-infra-keboola` pro Keboola-as-customer, `{org}/agnes-infra` pro další) s Terraform + GitHub Actions + SA JSON key. Každý zákazník má vlastní GCP projekt, vlastní Secret Manager, vlastní prod/dev VMs. Watchtower na VMs polluje GHCR pro auto-deploy. Branch-aware dev VMs přes pole `dev_instances` v tfvars.

**Tech Stack:** Terraform (google provider ~5.0), Docker Compose, Caddy (TLS), Watchtower, GHCR, Google Cloud (Compute Engine, Secret Manager, Cloud Storage, IAM), GitHub Actions, Argon2 (passwords), DuckDB.

---

## Závislosti mezi fázemi

```
Fáze 1 (MVP)  ──────────────────────────┐
   │                                    │
   ▼                                    ▼
Fáze 2 (TF modul + PD + rebuild)    Fáze 0 (Předpoklady)
   │
   ├─────────┬──────────┬──────────┐
   ▼         ▼          ▼          ▼
Fáze 3    Fáze 4     Fáze 5     Fáze 6
(TLS)   (Watchtower) (CI/CD)  (Template)
   │         │          │          │
   └─────────┴──────────┴──────────┘
              ▼
          Hotovo
```

Fáze 0 a 1 jsou sériové. Po Fázi 2 mohou 3/4/5 běžet paralelně. Fáze 6 používá výstupy 3/4/5.

---

## Fáze 0 — Předpoklady (manuální, mimo kód)

Tyto kroky vyžadují externí akce (oprávnění, Keboola UI). Musí být hotové před Fází 1.

### Task 0.1: Ověřit přístupová práva

- [ ] **Step 1: Ověřit, že máš `iam.serviceAccountAdmin` na <gcp-project>**

```bash
gcloud projects get-iam-policy <gcp-project> --format=json \
  | python3 -c "import json, sys; d=json.load(sys.stdin); \
      me='admin@example.com'; \
      roles=[b['role'] for b in d['bindings'] if any(me in m for m in b.get('members', []))]; \
      print('\n'.join(roles) if roles else 'NO DIRECT ROLES — check org-level or ask Petr (owner)')"
```

Expected: seznam rolí, nebo poznámka "NO DIRECT ROLES".

- [ ] **Step 2: Pokud chybí SA admin práva, požádat Petra o dočasný `roles/iam.serviceAccountAdmin` + `roles/resourcemanager.projectIamAdmin`**

Poslat mu odkaz na tuhle dokumentaci: https://cloud.google.com/iam/docs/understanding-roles#iam-roles

Napsat Petrovi ve Slacku / emailu: "Potřebuji dočasně roli `iam.serviceAccountAdmin` a `resourcemanager.projectIamAdmin` na projektu `<gcp-project>` pro vytvoření Agnes deploy SA. Zrušíme, jakmile bude hotovo."

- [ ] **Step 3: Ověřit, že image `ghcr.io/keboola/agnes-the-ai-analyst` je public**

```bash
gh api /orgs/keboola/packages/container/agnes-the-ai-analyst --jq '.visibility' 2>&1
```

Expected: `"public"`. Pokud `"private"`, změnit přes GitHub UI: Keboola org → Packages → agnes-the-ai-analyst → Package settings → Change visibility → Public.

### Task 0.2: Backup stávajících dat (safety net před Fází 2)

- [ ] **Step 1: Snapshot boot disku prod VM (obsahuje /data)**

```bash
gcloud compute disks snapshot data-analyst \
    --zone=europe-west1-b \
    --snapshot-names=data-analyst-pre-migration-$(date +%Y%m%d) \
    --project=<gcp-project>
```

Expected: `Created snapshot data-analyst-pre-migration-YYYYMMDD`.

- [ ] **Step 2: Ověřit snapshot**

```bash
gcloud compute snapshots list --project=<gcp-project> \
    --filter="name~pre-migration" --format="table(name, status, diskSizeGb, creationTimestamp)"
```

Expected: STATUS = READY, 30 GB.

---

## Fáze 1 — MVP: Odstřihnout od osobního forku, přejít na :stable image

**Goal fáze:** Prod VM `data-analyst` pulluje image z GHCR, nikoliv git pull z `ZdenekSrotyr/tmp_oss`. Tokeny jsou v Secret Manageru. Přepnutí je reverzibilní.

### Task 1.1: Přidat per-branch image tagging do release.yml

**Files:**
- Modify: `.github/workflows/release.yml:47-95`

- [ ] **Step 1: Number current state of meta step**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
grep -n "branch_slug\|feature_tag\|SLUG" .github/workflows/release.yml 2>&1 | head -5
```

Expected: žádné výsledky — pattern neexistuje, přidáme ho.

- [ ] **Step 2: Otevřít `.github/workflows/release.yml` a najít `Claim version tag` step**

Sekce má `id: meta`. Za řádkem `echo "short_sha=${SHORT_SHA}" >> "$GITHUB_OUTPUT"` (~ř. 90) přidat:

```yaml
          # Per-branch slug for dev images (only on feature branches)
          if [[ "${{ github.ref }}" != "refs/heads/main" ]]; then
            BRANCH_NAME="${GITHUB_REF#refs/heads/}"
            BRANCH_SLUG=$(echo "$BRANCH_NAME" | sed 's|^feature/||' | sed 's|[^a-zA-Z0-9-]|-|g' | tr '[:upper:]' '[:lower:]' | cut -c1-50)
            echo "branch_slug=${BRANCH_SLUG}" >> "$GITHUB_OUTPUT"
            echo "Branch slug: ${BRANCH_SLUG}"
          fi
```

- [ ] **Step 3: V `Build and push` stepu přidat branch-slug tag**

Najít `tags: |` blok (~ř. 110), nahradit za:

```yaml
          tags: |
            ghcr.io/${{ github.repository }}:${{ steps.meta.outputs.channel }}
            ghcr.io/${{ github.repository }}:${{ steps.meta.outputs.versioned_tag }}
            ghcr.io/${{ github.repository }}:sha-${{ steps.meta.outputs.short_sha }}
            ${{ steps.meta.outputs.channel == 'dev' && format('ghcr.io/{0}:dev-{1}', github.repository, steps.meta.outputs.branch_slug) || '' }}
```

Poslední řádek přidá `:dev-<branch-slug>` jen při pushech na feature branch.

- [ ] **Step 4: Syntax check workflow**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
gh workflow view release.yml 2>&1 | head -10
```

Expected: workflow info, žádné "Parse error".

- [ ] **Step 5: Commit**

```bash
git add .github/workflows/release.yml
git commit -m "ci: add per-branch image tag :dev-<slug> for branch-aware dev deploys"
```

### Task 1.2: Vytvořit GCP deploy service account

**Files:**
- Create: `scripts/bootstrap-gcp.sh`

- [ ] **Step 1: Vytvořit bootstrap skript**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
mkdir -p scripts
```

Write `scripts/bootstrap-gcp.sh`:

```bash
#!/usr/bin/env bash
# Bootstrap GCP projekt pro Agnes deployment.
# Jednorázové, idempotentní. Výstup = výpis secretů pro GitHub Actions.
#
# Usage: bootstrap-gcp.sh <GCP_PROJECT_ID> [SA_NAME]
# Pokud SA existuje, skript vygeneruje nový klíč a skončí.
set -euo pipefail

PROJECT_ID="${1:?Usage: $0 <GCP_PROJECT_ID> [SA_NAME=agnes-deploy]}"
SA_NAME="${2:-agnes-deploy}"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "=== Bootstrap GCP projekt: ${PROJECT_ID} ==="
gcloud config set project "${PROJECT_ID}" 1>/dev/null

echo "=== Enable APIs ==="
gcloud services enable \
    compute.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    secretmanager.googleapis.com \
    cloudresourcemanager.googleapis.com \
    storage.googleapis.com \
    --project="${PROJECT_ID}"

echo "=== Create deploy service account (if not exists) ==="
if ! gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" 2>/dev/null; then
    gcloud iam service-accounts create "${SA_NAME}" \
        --display-name="Agnes Terraform deploy" \
        --project="${PROJECT_ID}"
fi

echo "=== Grant roles ==="
for role in \
    compute.instanceAdmin.v1 \
    compute.securityAdmin \
    compute.networkAdmin \
    iam.serviceAccountUser \
    secretmanager.admin \
    storage.admin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${SA_EMAIL}" \
        --role="roles/${role}" \
        --condition=None \
        --quiet 1>/dev/null
done

echo "=== Create tfstate bucket (if not exists) ==="
BUCKET="agnes-${PROJECT_ID}-tfstate"
if ! gsutil ls -b "gs://${BUCKET}" 2>/dev/null; then
    gsutil mb -p "${PROJECT_ID}" -l europe-west1 -b on "gs://${BUCKET}"
    gsutil versioning set on "gs://${BUCKET}"
fi

echo "=== Generate SA key ==="
KEY_FILE="./${SA_NAME}-${PROJECT_ID}-key.json"
gcloud iam service-accounts keys create "${KEY_FILE}" \
    --iam-account="${SA_EMAIL}" \
    --project="${PROJECT_ID}"

echo ""
echo "=== HOTOVO ==="
echo ""
echo "SA email:           ${SA_EMAIL}"
echo "TF state bucket:    gs://${BUCKET}"
echo "SA key file:        ${KEY_FILE}"
echo ""
echo "DALŠÍ KROKY:"
echo "1. Pushni klíč do GitHub secretu privátního infra repa:"
echo "   gh secret set GCP_SA_KEY --repo <owner>/<repo> < ${KEY_FILE}"
echo "2. POTOM smaž klíč z lokálu:"
echo "   rm ${KEY_FILE}"
echo ""
```

- [ ] **Step 2: Udělat skript executable**

```bash
chmod +x scripts/bootstrap-gcp.sh
```

- [ ] **Step 3: Spustit skript na <gcp-project>**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
./scripts/bootstrap-gcp.sh <gcp-project>
```

Expected: na konci výpis "HOTOVO" + instrukce.

Pokud selže na "Permission denied": viz Task 0.1 step 2 (požádat Petra).

- [ ] **Step 4: Ověřit SA a bucket**

```bash
gcloud iam service-accounts list --project=<gcp-project> --filter="email~agnes-deploy" --format="value(email)"
gsutil ls -b gs://agnes-<gcp-project>-tfstate
```

Expected: SA email + bucket URL.

- [ ] **Step 5: Commit bootstrap skript**

```bash
git add scripts/bootstrap-gcp.sh
git commit -m "infra: add bootstrap-gcp.sh for per-customer GCP setup"
```

### Task 1.3: Nastavit tajemství v Secret Manageru

- [ ] **Step 1: Rotovat Keboola Storage token v Keboola UI**

Přihlásit se do Keboola UI (https://connection.us-east4.gcp.keboola.com/), sekce Settings → Master Tokens → vygenerovat nový token.

**Starý token zachovat aktivní, dokud nebude nový nasazený.**

- [ ] **Step 2: Uložit nový token do Secret Manageru**

```bash
read -s NEW_TOKEN
echo -n "$NEW_TOKEN" | gcloud secrets create keboola-storage-token \
    --data-file=- \
    --replication-policy=automatic \
    --project=<gcp-project>
unset NEW_TOKEN
```

Expected: `Created secret [keboola-storage-token]`.

- [ ] **Step 3: Vygenerovat a uložit JWT secret**

```bash
openssl rand -hex 32 | gcloud secrets create jwt-secret-key \
    --data-file=- \
    --replication-policy=automatic \
    --project=<gcp-project>
```

Expected: `Created secret [jwt-secret-key]`.

- [ ] **Step 4: Ověřit secrets**

```bash
gcloud secrets list --project=<gcp-project> --format="table(name, createTime)"
```

Expected: dva secrets — keboola-storage-token, jwt-secret-key.

- [ ] **Step 5: Přiřadit read access deploy SA**

```bash
for secret in keboola-storage-token jwt-secret-key; do
    gcloud secrets add-iam-policy-binding "$secret" \
        --member="serviceAccount:agnes-deploy@<gcp-project>.iam.gserviceaccount.com" \
        --role=roles/secretmanager.secretAccessor \
        --project=<gcp-project>
done
```

Expected: `Updated IAM policy` × 2.

### Task 1.4: Vytvořit skript, který na VM natáhne secrets ze Secret Manageru do .env

**Files:**
- Create: `scripts/fetch-env-from-secrets.sh`

- [ ] **Step 1: Napsat skript**

Write `scripts/fetch-env-from-secrets.sh`:

```bash
#!/usr/bin/env bash
# Stáhne secrets z GCP Secret Manageru a vytvoří .env pro Agnes.
# Spouští se jednorázově na VM během boot / deploy.
#
# Vyžaduje:
#   - gcloud CLI (už nainstalované na GCE default image)
#   - VM SA má roli roles/secretmanager.secretAccessor
set -euo pipefail

APP_DIR="${APP_DIR:-/home/deploy/app}"
ENV_FILE="${APP_DIR}/.env"

echo "Fetching secrets..."

KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token 2>&1)
JWT_KEY=$(gcloud secrets versions access latest --secret=jwt-secret-key 2>&1)

# Non-secret config (může zůstat v metadatě/startup-scriptu)
DATA_SOURCE="${DATA_SOURCE:-keboola}"
KEBOOLA_STACK_URL="${KEBOOLA_STACK_URL:-https://connection.us-east4.gcp.keboola.com/}"
SEED_ADMIN_EMAIL="${SEED_ADMIN_EMAIL:-admin@example.com}"
LOG_LEVEL="${LOG_LEVEL:-info}"
DATA_DIR="${DATA_DIR:-/data}"

cat > "${ENV_FILE}" <<EOF
JWT_SECRET_KEY=${JWT_KEY}
DATA_DIR=${DATA_DIR}
DATA_SOURCE=${DATA_SOURCE}
KEBOOLA_STORAGE_TOKEN=${KEBOOLA_TOKEN}
KEBOOLA_STACK_URL=${KEBOOLA_STACK_URL}
SEED_ADMIN_EMAIL=${SEED_ADMIN_EMAIL}
LOG_LEVEL=${LOG_LEVEL}
EOF

chmod 600 "${ENV_FILE}"
chown deploy:deploy "${ENV_FILE}" 2>/dev/null || true

echo "Wrote ${ENV_FILE} (chmod 600)"
```

- [ ] **Step 2: Chmod + commit**

```bash
chmod +x scripts/fetch-env-from-secrets.sh
git add scripts/fetch-env-from-secrets.sh
git commit -m "infra: add fetch-env-from-secrets.sh for VM-side secret retrieval"
```

### Task 1.5: Připravit prod docker-compose pro GHCR image

**Files:**
- Modify: `docker-compose.prod.yml`

- [ ] **Step 1: Přečíst současný docker-compose.prod.yml**

```bash
cat "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss/docker-compose.prod.yml"
```

Zaznamenat si strukturu (services, volumes).

- [ ] **Step 2: Ověřit, že prod overlay používá `image:` místo `build:`**

```bash
grep -E "^\s*(image|build):" docker-compose.prod.yml
```

Expected: řádek `image: ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}` (nebo podobně). Pokud chybí, přidat do `services.app`:

```yaml
services:
  app:
    image: ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}
    build: !reset null   # vypnout lokální build
```

A pro scheduler:

```yaml
  scheduler:
    image: ghcr.io/keboola/agnes-the-ai-analyst:${AGNES_TAG:-stable}
    build: !reset null
```

- [ ] **Step 3: Commit změn (pokud nějaké)**

```bash
git status docker-compose.prod.yml
# Pokud modified:
git add docker-compose.prod.yml
git commit -m "infra: prod compose pulls from GHCR via AGNES_TAG env (default :stable)"
```

### Task 1.6: Deploy MVP na prod VM data-analyst

**Tohle je destruktivní akce na prod. Předtím Task 0.2 (snapshot).**

- [ ] **Step 1: SSH na prod VM a zastavit kontejnery**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'cd /home/deploy/app && docker compose down'"
```

Expected: `Container app-app-1 Stopped`, `Container app-scheduler-1 Stopped`.

- [ ] **Step 2: Nastavit VM SA na deploy VM (jednorázově)**

```bash
# Ověřit aktuální SA
gcloud compute instances describe data-analyst --zone=europe-west1-b --project=<gcp-project> \
    --format="value(serviceAccounts[0].email)"
```

Pokud výstup `327445566538-compute@developer.gserviceaccount.com` (default SA), je to OK pro MVP — má cloud-platform scope a může číst secrets. Ve Fázi 4 (hardening) to přepneme na dedikovaný SA.

Přidat mu explicitně secretmanager.secretAccessor (idempotentní):

```bash
gcloud projects add-iam-policy-binding <gcp-project> \
    --member="serviceAccount:327445566538-compute@developer.gserviceaccount.com" \
    --role="roles/secretmanager.secretAccessor" \
    --condition=None
```

- [ ] **Step 3: Uploadnout fetch-env skript na VM**

```bash
gcloud compute scp \
    "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss/scripts/fetch-env-from-secrets.sh" \
    data-analyst:/tmp/fetch-env.sh \
    --zone=europe-west1-b --project=<gcp-project>
```

- [ ] **Step 4: Spustit fetch-env skript pod uživatelem deploy**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo install -m 755 -o deploy -g deploy /tmp/fetch-env.sh /home/deploy/app/fetch-env.sh && sudo -u deploy bash -c 'cd /home/deploy/app && ./fetch-env.sh'"
```

Expected: `Wrote /home/deploy/app/.env (chmod 600)`.

- [ ] **Step 5: Zkontrolovat .env na VM (bez vypisování hodnot)**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'ls -la /home/deploy/app/.env && wc -l /home/deploy/app/.env && cut -d= -f1 /home/deploy/app/.env'"
```

Expected: soubor 600 mode, 7 řádků, klíče: JWT_SECRET_KEY, DATA_DIR, DATA_SOURCE, KEBOOLA_STORAGE_TOKEN, KEBOOLA_STACK_URL, SEED_ADMIN_EMAIL, LOG_LEVEL.

- [ ] **Step 6: Aktualizovat docker-compose.yml konfiguraci na VM na pulling z GHCR**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'cd /home/deploy/app && git fetch origin feature/v2-fastapi-duckdb-docker-cli && git reset --hard origin/feature/v2-fastapi-duckdb-docker-cli'"
```

**Pozor:** VM má starý remote `ZdenekSrotyr/tmp_oss`. Tohle tedy nebude fungovat, pokud se ten repo smazal. Alternativa: nahradit origin remote za keboola/agnes-the-ai-analyst:

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'cd /home/deploy/app && git remote set-url origin https://github.com/keboola/agnes-the-ai-analyst.git && git fetch origin main && git reset --hard origin/main'"
```

Expected: HEAD is now at `<sha>` `<message>`.

- [ ] **Step 7: Pullnout image z GHCR a nastartovat s novým override**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'cd /home/deploy/app && export AGNES_TAG=stable && docker compose -f docker-compose.yml -f docker-compose.prod.yml pull && docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d'"
```

Expected: `Container app-app-1 Started`, `Container app-scheduler-1 Started`.

- [ ] **Step 8: Ověřit běh**

```bash
# Počkat 30 sekund
sleep 30
curl -s --max-time 10 http://<redacted-ip>:8000/api/health | python3 -m json.tool | head -10
```

Expected: `"status": "healthy"` nebo `"degraded"` (stale tables jsou OK). Ne `connection refused`.

- [ ] **Step 9: Ověřit, že app používá nový image**

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo docker inspect app-app-1 --format '{{.Config.Image}}'"
```

Expected: `ghcr.io/keboola/agnes-the-ai-analyst:stable` (ne `app-app`).

- [ ] **Step 10: Ověřit login**

```bash
curl -sS --max-time 5 -X POST http://<redacted-ip>:8000/auth/password/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"<password>"}' 2>&1 | python3 -c "import sys,json; d=json.load(sys.stdin); print('OK — role:', d.get('role'))"
```

Expected: `OK — role: admin`.

- [ ] **Step 11: Zapsat poznámku o nové .env strategii do dokumentace**

Add to `docs/DEPLOYMENT.md` (if not present) section "Production environment":

```markdown
## Production .env strategy

Secrets (KEBOOLA_STORAGE_TOKEN, JWT_SECRET_KEY) are fetched from GCP Secret Manager
by `scripts/fetch-env-from-secrets.sh` during VM boot. Non-secret config (STACK_URL,
SEED_ADMIN_EMAIL, LOG_LEVEL) is passed via env vars in the startup script.

To rotate a secret:
1. Add a new version via `gcloud secrets versions add ...`
2. SSH to VM and re-run `./fetch-env.sh`
3. Restart: `docker compose up -d --force-recreate app`
```

- [ ] **Step 12: Commit dokumentace**

```bash
git add docs/DEPLOYMENT.md
git commit -m "docs: document Secret Manager-backed .env for production"
```

### Task 1.7: Zopakovat MVP deploy na dev VM

- [ ] **Step 1: Opakovat Task 1.6 steps 1-10 proti data-analyst-dev VM**

Stejné příkazy, jen zaměnit `data-analyst` za `data-analyst-dev` a IP `<redacted-ip>` za `<redacted-ip>`.

- [ ] **Step 2: Verify**

```bash
curl -s --max-time 10 http://<redacted-ip>:8000/api/health | python3 -m json.tool | head -3
```

Expected: valid JSON s `"status"`.

### Task 1.8: Smazat osobní fork

- [ ] **Step 1: Odstranit deploy key z `ZdenekSrotyr/tmp_oss` (pokud existuje)**

```bash
gh api repos/ZdenekSrotyr/tmp_oss/keys 2>&1 | python3 -m json.tool
```

Pokud něco vrací, smazat: `gh api -X DELETE repos/ZdenekSrotyr/tmp_oss/keys/<id>`.

- [ ] **Step 2: Smazat repo**

```bash
gh repo delete ZdenekSrotyr/tmp_oss --yes
```

Expected: `✓ Deleted repository ZdenekSrotyr/tmp_oss`.

- [ ] **Step 3: Ověřit, že je fuč**

```bash
gh api repos/ZdenekSrotyr/tmp_oss 2>&1 | head -2
```

Expected: `Not Found (HTTP 404)`.

### Task 1.9: Invalidovat starý Keboola token

- [ ] **Step 1: V Keboola UI zrušit starý master token**

(Ruční krok v Keboola UI. Nový token už je v Secret Manageru z Task 1.3.)

Ověřit, že nová verze tokenu funguje:

```bash
curl -s --max-time 10 http://<redacted-ip>:8000/api/sync/status 2>&1 | python3 -m json.tool | head -20
```

Expected: nějaký valid JSON. Pokud `401 Unauthorized` nebo `Invalid token`, app ještě má cached starý token — restartovat:

```bash
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo -u deploy bash -c 'cd /home/deploy/app && docker compose restart app'"
```

### Task 1.10: Checkpoint — Fáze 1 hotová

- [ ] **Step 1: Přepnout heslo z `<password>` na něco silného**

Přes UI nebo:

```bash
read -s NEW_PASSWORD
TOKEN=$(curl -sS -X POST http://<redacted-ip>:8000/auth/password/login \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"<password>"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
# [Volba: použít admin endpoint pro změnu hesla, pokud existuje — jinak přes UI]
unset NEW_PASSWORD TOKEN
```

- [ ] **Step 2: Ověřit stav**

Zkontrolovat checklist:
- [ ] Prod VM `data-analyst` běží z `ghcr.io/...:stable`
- [ ] Dev VM `data-analyst-dev` běží z `ghcr.io/...:stable`
- [ ] Secrets v GCP Secret Manageru
- [ ] Heslo admin usera není výchozí
- [ ] `ZdenekSrotyr/tmp_oss` je smazaný
- [ ] Starý Keboola token je invalidován

---

## Fáze 2 — TF modul + persistent disk + F1 rebuild

**Goal fáze:** Keboola instance běží na VMs, kterou spravuje Terraform modul z `infra/modules/customer-instance/`. Data jsou na samostatném persistent disku. TF state v GCS bucketu.

### Task 2.1: Refactor `infra/main.tf` na modulární strukturu

**Files:**
- Create: `infra/modules/customer-instance/main.tf`
- Create: `infra/modules/customer-instance/variables.tf`
- Create: `infra/modules/customer-instance/outputs.tf`
- Create: `infra/modules/customer-instance/startup-script.sh`
- Delete: `infra/main.tf` (old monolith)
- Keep (upraveno): `infra/variables.tf`, `infra/outputs.tf`, `infra/terraform.tfvars.example`
- Create: `infra/examples/minimal/main.tf` (usage example)

- [ ] **Step 1: Vytvořit adresářovou strukturu**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
mkdir -p infra/modules/customer-instance
mkdir -p infra/examples/minimal
```

- [ ] **Step 2: Napsat `infra/modules/customer-instance/variables.tf`**

Write:

```hcl
variable "gcp_project_id" {
  description = "GCP project ID kde bude instance nasazená"
  type        = string
}

variable "region" {
  description = "GCP region"
  type        = string
  default     = "europe-west1"
}

variable "zone" {
  description = "GCP zone"
  type        = string
  default     = "europe-west1-b"
}

variable "customer_name" {
  description = "Krátké identifikátor zákazníka (např. keboola, another-customer). Použije se v prefixu resourců."
  type        = string
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.customer_name))
    error_message = "customer_name musí být lowercase, začínat písmenem, 2-21 znaků."
  }
}

variable "prod_instance" {
  description = "Prod VM konfigurace"
  type = object({
    name         = string
    machine_type = optional(string, "e2-small")
    disk_size_gb = optional(number, 30)
    data_disk_gb = optional(number, 50)
    image_tag    = optional(string, "stable")
    upgrade_mode = optional(string, "auto")
    tls_mode     = optional(string, "caddy")
    domain       = optional(string, "")
  })
}

variable "dev_instances" {
  description = "Seznam dev VMs. Prázdné pole = žádné dev VMs."
  type = list(object({
    name         = string
    machine_type = optional(string, "e2-small")
    image_tag    = optional(string, "dev")
  }))
  default = []
}

variable "seed_admin_email" {
  description = "Email prvního admin usera"
  type        = string
}

variable "data_source" {
  description = "Typ data source — keboola | bigquery | csv"
  type        = string
  default     = "keboola"
}

variable "keboola_stack_url" {
  description = "Keboola Stack URL (pokud data_source = keboola)"
  type        = string
  default     = ""
}

variable "image_repo" {
  description = "Docker image repo"
  type        = string
  default     = "ghcr.io/keboola/agnes-the-ai-analyst"
}
```

- [ ] **Step 3: Napsat `infra/modules/customer-instance/main.tf`**

Write:

```hcl
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 5.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.0"
    }
  }
}

locals {
  all_instances = concat(
    [merge(var.prod_instance, { role = "prod" })],
    [for d in var.dev_instances : merge(d, {
      role         = "dev"
      disk_size_gb = 30
      data_disk_gb = 20
      upgrade_mode = "auto"
      tls_mode     = "caddy"
      domain       = ""
    })]
  )
}

# --- Secrets ---

resource "google_secret_manager_secret" "jwt" {
  secret_id = "agnes-${var.customer_name}-jwt-secret"
  project   = var.gcp_project_id
  replication { auto {} }
}

resource "random_password" "jwt" {
  length  = 48
  special = false
}

resource "google_secret_manager_secret_version" "jwt" {
  secret      = google_secret_manager_secret.jwt.id
  secret_data = random_password.jwt.result
}

# Keboola token — manuálně vytvořený secret (tenhle TF ho jen referenční).
data "google_secret_manager_secret_version" "keboola_token" {
  count   = var.data_source == "keboola" ? 1 : 0
  secret  = "keboola-storage-token"
  project = var.gcp_project_id
}

# --- VM service account (dedikovaný, bez cloud-platform scope) ---

resource "google_service_account" "vm" {
  account_id   = "agnes-${var.customer_name}-vm"
  display_name = "Agnes VM runtime SA (${var.customer_name})"
  project      = var.gcp_project_id
}

resource "google_project_iam_member" "vm_secrets" {
  project = var.gcp_project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.vm.email}"
}

# --- Network ---

resource "google_compute_firewall" "web" {
  name    = "agnes-${var.customer_name}-allow-web"
  project = var.gcp_project_id
  network = "default"

  allow {
    protocol = "tcp"
    ports    = ["22", "80", "443", "8000"]
  }

  source_ranges = ["<redacted-ip>/0"]
  target_tags   = ["agnes-${var.customer_name}"]
}

# --- Persistent data disks + VMs (prod + dev) ---

resource "google_compute_disk" "data" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-data"
  project = var.gcp_project_id
  zone    = var.zone
  size    = each.value.data_disk_gb
  type    = "pd-ssd"
}

resource "google_compute_address" "ip" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name    = "${each.value.name}-ip"
  project = var.gcp_project_id
  region  = var.region
}

resource "google_compute_instance" "vm" {
  for_each = { for inst in local.all_instances : inst.name => inst }

  name         = each.value.name
  project      = var.gcp_project_id
  machine_type = each.value.machine_type
  zone         = var.zone
  tags         = ["agnes-${var.customer_name}"]

  boot_disk {
    initialize_params {
      image = "ubuntu-os-cloud/ubuntu-2404-lts-amd64"
      size  = each.value.disk_size_gb
      type  = "pd-ssd"
    }
  }

  attached_disk {
    source      = google_compute_disk.data[each.key].self_link
    device_name = "data"
  }

  network_interface {
    network = "default"
    access_config {
      nat_ip = google_compute_address.ip[each.key].address
    }
  }

  metadata = {
    enable-oslogin = "TRUE"
  }

  metadata_startup_script = templatefile("${path.module}/startup-script.sh", {
    customer_name     = var.customer_name
    image_repo        = var.image_repo
    image_tag         = each.value.image_tag
    upgrade_mode      = each.value.upgrade_mode
    tls_mode          = each.value.tls_mode
    domain            = each.value.domain
    data_source       = var.data_source
    keboola_stack_url = var.keboola_stack_url
    seed_admin_email  = var.seed_admin_email
    role              = each.value.role
  })

  service_account {
    email  = google_service_account.vm.email
    scopes = ["cloud-platform"]
  }

  labels = {
    app      = "agnes"
    customer = var.customer_name
    role     = each.value.role
    managed  = "terraform"
  }

  lifecycle {
    ignore_changes = [metadata_startup_script]
  }
}
```

- [ ] **Step 4: Napsat `infra/modules/customer-instance/startup-script.sh`**

Write:

```bash
#!/bin/bash
# Agnes VM startup script.
# Idempotentní — spustí se při každém boot.
set -euo pipefail
exec > /var/log/agnes-startup.log 2>&1

CUSTOMER_NAME="${customer_name}"
IMAGE_REPO="${image_repo}"
IMAGE_TAG="${image_tag}"
UPGRADE_MODE="${upgrade_mode}"
TLS_MODE="${tls_mode}"
DOMAIN="${domain}"
DATA_SOURCE="${data_source}"
KEBOOLA_STACK_URL="${keboola_stack_url}"
SEED_ADMIN_EMAIL="${seed_admin_email}"
ROLE="${role}"

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup ==="

# --- 1. Docker (install if missing) ---
if ! command -v docker &>/dev/null; then
    curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version &>/dev/null; then
    apt-get update && apt-get install -y docker-compose-plugin
fi

# --- 2. Persistent disk mount ---
DATA_DEV="/dev/disk/by-id/google-data"
DATA_MNT="/data"
if [ -b "$DATA_DEV" ]; then
    if ! blkid "$DATA_DEV" | grep -q ext4; then
        mkfs.ext4 -F "$DATA_DEV"
    fi
    mkdir -p "$DATA_MNT"
    mountpoint -q "$DATA_MNT" || mount -o discard,defaults "$DATA_DEV" "$DATA_MNT"
    grep -q "$DATA_DEV" /etc/fstab || echo "$DATA_DEV $DATA_MNT ext4 discard,defaults,nofail 0 2" >> /etc/fstab
    mkdir -p "$DATA_MNT/state" "$DATA_MNT/analytics" "$DATA_MNT/extracts"
fi

# --- 3. App directory (pro docker-compose.yml) ---
APP_DIR="/opt/agnes"
mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Fetch minimal docker-compose — z public repa na jejich tagu
curl -fsSL "https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.yml" \
    -o docker-compose.yml
curl -fsSL "https://raw.githubusercontent.com/keboola/agnes-the-ai-analyst/main/docker-compose.prod.yml" \
    -o docker-compose.prod.yml

# --- 4. Fetch secrets from Secret Manager ---
KEBOOLA_TOKEN=""
if [ "$DATA_SOURCE" = "keboola" ]; then
    KEBOOLA_TOKEN=$(gcloud secrets versions access latest --secret=keboola-storage-token 2>/dev/null || echo "")
fi
JWT_KEY=$(gcloud secrets versions access latest --secret=agnes-$CUSTOMER_NAME-jwt-secret)

cat > "$APP_DIR/.env" <<EOF
JWT_SECRET_KEY=$JWT_KEY
DATA_DIR=$DATA_MNT
DATA_SOURCE=$DATA_SOURCE
KEBOOLA_STORAGE_TOKEN=$KEBOOLA_TOKEN
KEBOOLA_STACK_URL=$KEBOOLA_STACK_URL
SEED_ADMIN_EMAIL=$SEED_ADMIN_EMAIL
LOG_LEVEL=info
DOMAIN=$DOMAIN
AGNES_TAG=$IMAGE_TAG
EOF
chmod 600 "$APP_DIR/.env"

# --- 5. Start Agnes ---
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# --- 6. Watchtower (auto pull nových image) ---
if [ "$UPGRADE_MODE" = "auto" ]; then
    docker run -d --name watchtower --restart=unless-stopped \
        -v /var/run/docker.sock:/var/run/docker.sock \
        containrrr/watchtower \
        --interval 300 --cleanup \
        $(docker ps --filter "ancestor=$IMAGE_REPO:$IMAGE_TAG" --format "{{.Names}}") 2>/dev/null || true
fi

echo "=== [Agnes $CUSTOMER_NAME $ROLE] Startup complete ==="
docker compose ps
```

- [ ] **Step 5: Napsat `infra/modules/customer-instance/outputs.tf`**

Write:

```hcl
output "instance_ips" {
  description = "Mapa { name → external IP }"
  value       = { for k, v in google_compute_address.ip : k => v.address }
}

output "prod_ip" {
  description = "External IP prod instance"
  value       = google_compute_address.ip[var.prod_instance.name].address
}

output "vm_service_account" {
  description = "Email VM SA (pro další IAM bindings, např. BigQuery)"
  value       = google_service_account.vm.email
}

output "jwt_secret_name" {
  description = "Plný název JWT secretu v Secret Manageru"
  value       = google_secret_manager_secret.jwt.name
}
```

- [ ] **Step 6: Smazat starý `infra/main.tf` a uložit si ho jako backup**

```bash
mv infra/main.tf infra/main.tf.backup-pre-module
```

- [ ] **Step 7: Vytvořit `infra/examples/minimal/main.tf`**

Write:

```hcl
# Minimal example: single-VM Agnes deploy.
# Pro OSS self-hoster, co nechce ani persistent disk ani dev VM.
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = "europe-west1"
}

variable "gcp_project_id" {
  type = string
}

module "agnes" {
  source = "../../modules/customer-instance"

  gcp_project_id   = var.gcp_project_id
  customer_name    = "self-hosted"
  seed_admin_email = "admin@example.com"

  prod_instance = {
    name         = "agnes"
    data_disk_gb = 30
  }

  dev_instances = []

  data_source = "keboola"
}

output "agnes_ip" {
  value = module.agnes.prod_ip
}
```

- [ ] **Step 8: Smazat `infra/variables.tf`, `infra/outputs.tf`, `infra/terraform.tfvars.example` (už patří do modulu / examples)**

```bash
# Backup si udělat
mv infra/variables.tf infra/variables.tf.backup-pre-module
mv infra/outputs.tf infra/outputs.tf.backup-pre-module
mv infra/terraform.tfvars.example infra/terraform.tfvars.example.backup-pre-module
```

- [ ] **Step 9: `terraform init` + `validate` v example**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss/infra/examples/minimal"
terraform init -backend=false
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 10: Commit**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
git add infra/modules/ infra/examples/ 
git add -u infra/  # pro mv backupy
git commit -m "infra: extract customer-instance Terraform module; add minimal example"
```

### Task 2.2: Tag prvního release TF modulu

- [ ] **Step 1: Otevřít PR z feature branch do main**

```bash
git push origin feature/v2-fastapi-duckdb-docker-cli
gh pr create --title "feat: multi-customer deployment (Fáze 1-2)" \
    --body "Implements Phases 1-2 of docs/superpowers/plans/2026-04-21-multi-customer-deployment.md"
```

- [ ] **Step 2: Po mergi do main vytvořit tag `infra-v1.0.0`**

```bash
git checkout main
git pull
git tag -a infra-v1.0.0 -m "Initial customer-instance module release"
git push origin infra-v1.0.0
```

### Task 2.3: Založit privátní repo `keboola/agnes-infra-keboola` (manuálně)

**Tohle je krok mimo tento repo. Plán jen popisuje.**

- [ ] **Step 1: Vytvořit prázdný privátní repo**

```bash
gh repo create keboola/agnes-infra-keboola --private --description "Agnes deployment — Keboola internal instance"
```

- [ ] **Step 2: Klonovat lokálně vedle tohohle repa**

```bash
cd ~/Library/Mobile\ Documents/com\~apple\~CloudDocs/Sources/VsCode/component_factory/
gh repo clone keboola/agnes-infra-keboola
cd agnes-infra-keboola
```

- [ ] **Step 3: Vytvořit strukturu**

```bash
mkdir -p terraform .github/workflows config

# Terraform root
cat > terraform/main.tf <<'EOF'
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
  backend "gcs" {
    bucket = "agnes-<gcp-project>-tfstate"
    prefix = "keboola"
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.region
  zone    = var.zone
}

module "agnes" {
  source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.0.0"

  gcp_project_id    = var.gcp_project_id
  region            = var.region
  zone              = var.zone
  customer_name     = "keboola"
  seed_admin_email  = var.seed_admin_email
  data_source       = "keboola"
  keboola_stack_url = var.keboola_stack_url
  prod_instance     = var.prod_instance
  dev_instances     = var.dev_instances
}

output "prod_ip" { value = module.agnes.prod_ip }
output "instance_ips" { value = module.agnes.instance_ips }
EOF

cat > terraform/variables.tf <<'EOF'
variable "gcp_project_id"    { type = string }
variable "region"            { type = string, default = "europe-west1" }
variable "zone"              { type = string, default = "europe-west1-b" }
variable "seed_admin_email"  { type = string }
variable "keboola_stack_url" { type = string }
variable "prod_instance"     { type = any }
variable "dev_instances"     { type = any, default = [] }
EOF

cat > terraform/terraform.tfvars.example <<'EOF'
gcp_project_id    = "<gcp-project>"
seed_admin_email  = "admin@example.com"
keboola_stack_url = "https://connection.us-east4.gcp.keboola.com/"

prod_instance = {
  name         = "agnes-prod"
  machine_type = "e2-small"
  data_disk_gb = 50
  image_tag    = "stable"
  upgrade_mode = "auto"
  tls_mode     = "caddy"
  domain       = ""
}

dev_instances = [
  { name = "agnes-dev", image_tag = "dev" }
]
EOF

cat > terraform/.gitignore <<'EOF'
terraform.tfvars
*.tfstate
*.tfstate.*
.terraform/
.terraform.lock.hcl
EOF

cp terraform/terraform.tfvars.example terraform/terraform.tfvars
# Edit terraform.tfvars on real values if they differ
```

- [ ] **Step 4: Initial commit**

```bash
git add .
git commit -m "initial: Keboola-as-customer Agnes deployment"
git push -u origin main
```

- [ ] **Step 5: Uploadnout GCP_SA_KEY jako GitHub secret**

```bash
# Klíč vytvořený v Task 1.2 step 3
gh secret set GCP_SA_KEY --repo keboola/agnes-infra-keboola \
    < ../tmp_oss/agnes-deploy-<gcp-project>-key.json
```

**Poznámka:** Pokud klíč ne už smazal, re-generate: `gcloud iam service-accounts keys create ...`.

- [ ] **Step 6: První terraform init + plan (lokálně, abychom viděli diff)**

```bash
cd terraform
export GOOGLE_APPLICATION_CREDENTIALS="../agnes-deploy-key.json"
terraform init
terraform plan
```

Expected: `Plan: N to add, 0 to change, 0 to destroy.` (N ~ 15-20 resources)

Zkontrolovat plán: žádné `destroy` na existujících `data-analyst` / `data-analyst-dev` (to teprve poté, co bude nové nahoře).

### Task 2.4: Migrace dat ze starých VMs na nové (bez downtime risku)

**Strategy:** Zachovat staré VMs běžící. Terraform vytvoří **nové** VMs s jinými jmény (`agnes-prod`, `agnes-dev`). Data se zkopírují. Poté přepneme DNS/IP (nebo jen komunikujeme novou IP) a staré VMs smažeme.

- [ ] **Step 1: Snapshot starého /data**

Už máme z Task 0.2. Pokud je snapshot starší než 24 h, udělat nový:

```bash
gcloud compute disks snapshot data-analyst \
    --zone=europe-west1-b \
    --snapshot-names=data-analyst-migration-$(date +%Y%m%d-%H%M) \
    --project=<gcp-project>
```

- [ ] **Step 2: Terraform apply — vytvoří nové VMs (`agnes-prod`, `agnes-dev`) vedle starých**

```bash
cd ~/.../agnes-infra-keboola/terraform
terraform apply
# Type 'yes' to confirm
```

Expected: ~15-20 resources created, ~5 min. Outputs: `prod_ip`, `instance_ips`.

- [ ] **Step 3: Zkopírovat data ze starého boot-disku na nový persistent disk**

Nové VMs mají prázdný `/data`. Musíme do něj nakopírovat stav z `data-analyst` VM.

Nejjednodušší cesta: `rsync` mezi VM přes SSH.

```bash
# SSH na nové prod VM
NEW_PROD_IP=$(cd ~/.../agnes-infra-keboola/terraform && terraform output -raw prod_ip)

# Zkopírovat SSH klíč na starou VM, aby mohla mít přístup na novou
# (nebo použít oslogin → další prerekvizita)

# Alternativa: udělat z druhé strany — SSH na starou VM, rsync na novou
gcloud compute ssh data-analyst --zone=europe-west1-b --project=<gcp-project> --command="sudo docker compose -f /home/deploy/app/docker-compose.yml -f /home/deploy/app/docker-compose.prod.yml down"

# Rsync přes gcloud compute scp recursive (funguje jen z lokálu)
gcloud compute scp --recurse --zone=europe-west1-b --project=<gcp-project> \
    data-analyst:/home/deploy/app/data-volume/ \
    agnes-prod:/data/

# Spustit app na nové VM znovu
gcloud compute ssh agnes-prod --zone=europe-west1-b --project=<gcp-project> --command="sudo docker compose -f /opt/agnes/docker-compose.yml -f /opt/agnes/docker-compose.prod.yml restart"
```

**Alternativně (čistěji):** restore ze snapshotu přes `gcloud compute disks create --source-snapshot`, pak attach místo prázdného data disku.

- [ ] **Step 4: Ověřit nový prod**

```bash
NEW_PROD_IP=$(cd ~/.../agnes-infra-keboola/terraform && terraform output -raw prod_ip)
curl -s --max-time 10 "http://$NEW_PROD_IP:8000/api/health" | python3 -m json.tool | head -10
```

Expected: healthy / degraded, tables visible.

- [ ] **Step 5: Ověřit login na novém prod**

```bash
curl -sS -X POST "http://$NEW_PROD_IP:8000/auth/password/login" \
  -H "Content-Type: application/json" \
  -d '{"email":"admin@example.com","password":"<nové silné heslo z Task 1.10>"}' \
  | python3 -c "import sys,json;print('OK' if json.load(sys.stdin).get('role')=='admin' else 'FAIL')"
```

Expected: `OK`

- [ ] **Step 6: Zopakovat pro dev VM (`agnes-dev`)**

Stejné kroky 1-5.

- [ ] **Step 7: Vypnout staré VMs (zatím NEmazat — jen stop)**

```bash
gcloud compute instances stop data-analyst --zone=europe-west1-b --project=<gcp-project>
gcloud compute instances stop data-analyst-dev --zone=europe-west1-b --project=<gcp-project>
```

- [ ] **Step 8: Ověřit, že nový prod běží minimálně 24 h bez problému**

```bash
# Poznámka v kalendáři / Slacku: "check agnes-prod health in 24h"
curl -s "http://$NEW_PROD_IP:8000/api/health" | python3 -m json.tool
```

- [ ] **Step 9: Po 24h stability smazat staré VMs + jejich disky + statické IP**

```bash
gcloud compute instances delete data-analyst --zone=europe-west1-b --project=<gcp-project> --quiet
gcloud compute instances delete data-analyst-dev --zone=europe-west1-b --project=<gcp-project> --quiet

gcloud compute disks delete data-analyst --zone=europe-west1-b --project=<gcp-project> --quiet 2>&1 || true
gcloud compute disks delete data-analyst-dev --zone=europe-west1-b --project=<gcp-project> --quiet 2>&1 || true

gcloud compute addresses delete data-analyst-ip --region=europe-west1 --project=<gcp-project> --quiet 2>&1 || true
```

- [ ] **Step 10: Checkpoint — Fáze 2 hotová**

Checklist:
- [ ] Terraform modul v `infra/modules/customer-instance/`
- [ ] `keboola/agnes-infra-keboola` privátní repo existuje, `terraform apply` funguje
- [ ] Prod VM `agnes-prod` běží s persistent diskem
- [ ] Dev VM `agnes-dev` běží
- [ ] Data zmigrovaná, login funguje
- [ ] Staré VMs smazané, projekt vyčištěný

**Po Fázi 2 lze pokračovat paralelně Fázemi 3, 4, 5.**

---

## Fáze 3 — TLS přes Caddy

**Goal fáze:** Agnes je dostupná na HTTPS s automatickým Let's Encrypt certifikátem. Cookie `secure=True` funguje.

### Task 3.1: Přidat Caddy service do docker-compose

**Files:**
- Create: `Caddyfile` (v public repu root)
- Modify: `docker-compose.prod.yml` (přidat caddy service)

- [ ] **Step 1: Vytvořit Caddyfile**

Write `Caddyfile`:

```
# Agnes reverse proxy with automatic Let's Encrypt.
# Config přes ENV vars: AGNES_DOMAIN, ACME_EMAIL.

{$AGNES_DOMAIN} {
    # Health check endpoint bez TLS redirect (pro smoke testy interně)
    @health path /api/health
    
    encode gzip
    
    reverse_proxy app:8000 {
        header_up X-Forwarded-Proto https
    }
    
    tls {$ACME_EMAIL}
    
    log {
        output stdout
        format json
    }
}

# Fallback pro IP access (bez HTTPS, bez cert)
:80 {
    reverse_proxy app:8000
}
```

- [ ] **Step 2: Přidat caddy do `docker-compose.prod.yml`**

Add to `services` (pokud už tam není):

```yaml
  caddy:
    image: caddy:2-alpine
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    environment:
      AGNES_DOMAIN: ${AGNES_DOMAIN:-:80}
      ACME_EMAIL: ${ACME_EMAIL:-admin@example.com}
    depends_on:
      - app
    profiles:
      - tls   # nezapne se bez --profile tls

volumes:
  caddy_data:
  caddy_config:
```

- [ ] **Step 3: Aktualizovat modul — předat `tls_mode` do startup-script**

V `infra/modules/customer-instance/startup-script.sh` najít sekci `# --- 5. Start Agnes ---` a rozšířit:

```bash
# --- 5. Start Agnes ---
COMPOSE_PROFILES=""
if [ "$TLS_MODE" = "caddy" ] && [ -n "$DOMAIN" ]; then
    COMPOSE_PROFILES="--profile tls"
    # Další ENV pro Caddy
    {
        echo "AGNES_DOMAIN=$DOMAIN"
        echo "ACME_EMAIL=admin@$${DOMAIN#*.}"
    } >> "$APP_DIR/.env"
fi

docker compose -f docker-compose.yml -f docker-compose.prod.yml $COMPOSE_PROFILES pull
docker compose -f docker-compose.yml -f docker-compose.prod.yml $COMPOSE_PROFILES up -d
```

- [ ] **Step 4: Commit changes v public repu**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
git add Caddyfile docker-compose.prod.yml infra/modules/customer-instance/startup-script.sh
git commit -m "feat(tls): add Caddy reverse proxy with Let's Encrypt support"
```

- [ ] **Step 5: Tag nového releasu modulu**

```bash
# Po mergi PR do main
git checkout main && git pull
git tag -a infra-v1.1.0 -m "Add TLS support via Caddy"
git push origin infra-v1.1.0
```

### Task 3.2: Zapnout TLS pro Keboola instanci

**Tohle vyžaduje DNS záznam. Pokud nemáš doménu, skip a zůstaň na :8000.**

- [ ] **Step 1: V `keboola/agnes-infra-keboola/terraform/terraform.tfvars` nastavit doménu**

Pokud máme `agnes.keboola.com` (ověřit u IT), edit:

```hcl
prod_instance = {
  name     = "agnes-prod"
  # ...
  tls_mode = "caddy"
  domain   = "agnes.keboola.com"
}
```

A v `main.tf` bumpnout module ref:

```hcl
source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.1.0"
```

- [ ] **Step 2: Terraform apply**

```bash
cd ~/.../agnes-infra-keboola/terraform
terraform apply
```

- [ ] **Step 3: Nastavit DNS A record `agnes.keboola.com` → prod_ip**

Ruční krok (potřebuje přístup do Keboola DNS). Výstup `prod_ip` je IP.

- [ ] **Step 4: Počkat na DNS propagation + LE cert**

```bash
until nslookup agnes.keboola.com | grep -q "$(terraform output -raw prod_ip)"; do sleep 30; done
sleep 60  # čas na LE cert issuance
curl -sSI --max-time 10 https://agnes.keboola.com | head -5
```

Expected: `HTTP/2 200` (ne 301, ne TLS error).

---

## Fáze 4 — Watchtower (dev VM auto-deploy), OS Login, VM SA

**Goal fáze:** Dev VMs auto-pullují nové image. OS Login pro SSH (bez osobního klíče). Dedikovaný VM SA.

### Task 4.1: Watchtower integrace (už v Task 2 startup-script, zde jen ověření)

- [ ] **Step 1: SSH na dev VM a ověřit, že watchtower běží**

```bash
gcloud compute ssh agnes-dev --zone=europe-west1-b --project=<gcp-project> --command="sudo docker ps | grep watchtower"
```

Expected: container `watchtower` STATUS `Up X minutes`.

- [ ] **Step 2: Otestovat auto-deploy: pushnout drobnou změnu na feature branch, počkat**

```bash
# V public repu
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
git checkout -b feature/watchtower-test
echo "# test" >> README.md
git add README.md
git commit -m "test: trigger :dev image rebuild"
git push origin feature/watchtower-test
```

Počkat ~ 5-10 min (CI build + watchtower poll interval 5 min).

```bash
# Kontrola image sha na dev VM
gcloud compute ssh agnes-dev --zone=europe-west1-b --project=<gcp-project> \
    --command="sudo docker inspect app-app-1 --format '{{.Image}}' && sudo docker image inspect \$(sudo docker inspect app-app-1 --format '{{.Image}}') --format '{{.Created}}'"
```

Expected: Created timestamp v posledních ~ 10 minutách.

### Task 4.2: OS Login

- [ ] **Step 1: Ověřit, že modul nastavuje `enable-oslogin=TRUE`** 

Už je v `infra/modules/customer-instance/main.tf`:

```hcl
metadata = {
  enable-oslogin = "TRUE"
}
```

- [ ] **Step 2: Zkontrolovat, že uživatelé mají `roles/compute.osAdminLogin` na projektu**

```bash
gcloud projects get-iam-policy <gcp-project> \
    --flatten="bindings[].members" \
    --filter="bindings.role=roles/compute.osAdminLogin" \
    --format="value(bindings.members)"
```

Pokud prázdné, přidat:

```bash
gcloud projects add-iam-policy-binding <gcp-project> \
    --member=user:admin@example.com \
    --role=roles/compute.osAdminLogin
```

- [ ] **Step 3: Test SSH přes OS Login**

```bash
gcloud compute ssh agnes-prod --zone=europe-west1-b --project=<gcp-project> --command="whoami"
```

Expected: username ve formátu `zdenek_srotyr_keboola_com` (OS Login generated).

### Task 4.3: VM SA už má správný scope (ověřit)

- [ ] **Step 1: Ověřit, že VM SA má jen secretmanager.secretAccessor**

```bash
gcloud projects get-iam-policy <gcp-project> \
    --flatten="bindings[].members" \
    --filter="bindings.members:agnes-keboola-vm@" \
    --format="value(bindings.role)"
```

Expected: `roles/secretmanager.secretAccessor` (jen tohle).

---

## Fáze 5 — CI/CD v privátním infra repu

**Goal fáze:** PR v `keboola/agnes-infra-keboola` spustí `terraform plan`; merge → `terraform apply`. Prod aplikuje přes environment protection s reviewerem.

### Task 5.1: plan.yml workflow

**Files (v `keboola/agnes-infra-keboola` repu):**
- Create: `.github/workflows/plan.yml`

- [ ] **Step 1: Napsat plan.yml**

```yaml
name: Terraform Plan

on:
  pull_request:
    paths:
      - 'terraform/**'

permissions:
  contents: read
  pull-requests: write

jobs:
  plan:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: terraform
    steps:
      - uses: actions/checkout@v5

      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}

      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ~1.7

      - run: terraform init
      - run: terraform fmt -check
      - id: plan
        run: |
          terraform plan -no-color -out=tfplan 2>&1 | tee plan.txt
          echo "status=$(echo $? )" >> $GITHUB_OUTPUT

      - uses: actions/github-script@v7
        if: always()
        with:
          script: |
            const fs = require('fs');
            const plan = fs.readFileSync('terraform/plan.txt', 'utf8').slice(0, 60000);
            const body = `### Terraform plan\n\n\`\`\`\n${plan}\n\`\`\``;
            github.rest.issues.createComment({
              issue_number: context.issue.number,
              owner: context.repo.owner,
              repo: context.repo.repo,
              body: body
            });
```

- [ ] **Step 2: Commit**

```bash
cd ~/.../agnes-infra-keboola
git add .github/workflows/plan.yml
git commit -m "ci: add terraform plan on PR"
git push
```

### Task 5.2: apply.yml workflow s environment protection

**Files:**
- Create: `.github/workflows/apply.yml`

- [ ] **Step 1: Napsat apply.yml**

```yaml
name: Terraform Apply

on:
  push:
    branches: [main]
    paths:
      - 'terraform/**'
  workflow_dispatch: {}

permissions:
  contents: read

jobs:
  apply-dev:
    runs-on: ubuntu-latest
    environment: dev     # no protection
    defaults:
      run:
        working-directory: terraform
    steps:
      - uses: actions/checkout@v5
      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ~1.7
      - run: terraform init
      - run: terraform apply -auto-approve -target='module.agnes.google_compute_instance.vm["agnes-dev"]'

  apply-prod:
    needs: apply-dev
    runs-on: ubuntu-latest
    environment: prod    # protected — requires reviewer
    defaults:
      run:
        working-directory: terraform
    steps:
      - uses: actions/checkout@v5
      - uses: google-github-actions/auth@v2
        with:
          credentials_json: ${{ secrets.GCP_SA_KEY }}
      - uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: ~1.7
      - run: terraform init
      - run: terraform apply -auto-approve
      
      - name: Smoke test
        run: |
          PROD_IP=$(terraform output -raw prod_ip)
          for i in 1 2 3 4 5; do
            if curl -sf "http://$PROD_IP:8000/api/health" >/dev/null; then
              echo "Healthy"; exit 0
            fi
            sleep 15
          done
          echo "Health check failed"; exit 1
```

- [ ] **Step 2: V GitHub UI nastavit environmenty**

Navigovat do `keboola/agnes-infra-keboola` → Settings → Environments → New environment:

- **dev**: žádná protection
- **prod**:
  - Required reviewers: @ZdenekSrotyr (nebo @keboola-ops-team)
  - Wait timer: 5 min
  - Deployment branches: Selected branches → `main`

- [ ] **Step 3: Commit workflow**

```bash
git add .github/workflows/apply.yml
git commit -m "ci: add terraform apply with dev/prod environments and smoke test"
git push
```

- [ ] **Step 4: Test flow — otevřít dummy PR, sledovat plan, merge, apply**

```bash
git checkout -b test/ci-flow
# trivial edit in tfvars, např. přidat dev VM
echo "# ci flow test" >> terraform/README.md
git add terraform/README.md
git commit -m "test: CI flow"
git push origin test/ci-flow
gh pr create --title "test: CI flow" --body "Testing plan → apply flow"
```

V PR:
1. Počkat na plan.yml → komentář s plánem
2. Schválit + merge
3. Sledovat apply-dev (auto), pak apply-prod (čeká na reviewera)
4. Schválit prod deploy
5. Ověřit smoke test PASS

### Task 5.3: Rotovat SA key (z lokálního -> jen v GH secret)

- [ ] **Step 1: Smazat lokální SA key**

```bash
rm ~/.../agnes-deploy-<gcp-project>-key.json
```

- [ ] **Step 2: Na GCP smazat starý klíč (key rotation)**

```bash
# Seznam klíčů
gcloud iam service-accounts keys list \
    --iam-account=agnes-deploy@<gcp-project>.iam.gserviceaccount.com \
    --project=<gcp-project>
```

Po ověření, že GH Actions s novým klíčem funguje (po úspěšném prvním apply), smazat starý.

---

## Fáze 6 — Template repo + onboarding playbook

**Goal fáze:** Druhý zákazník (another-customer) se dá nasadit za < 1 hodinu.

### Task 6.1: Vytvořit `keboola/agnes-infra-template`

- [ ] **Step 1: Založit prázdný repo jako template**

```bash
gh repo create keboola/agnes-infra-template --public --description "Template for Agnes per-customer infrastructure" -c
cd ~/Library/Mobile\ Documents/com\~apple\~CloudDocs/Sources/VsCode/component_factory/
gh repo clone keboola/agnes-infra-template
cd agnes-infra-template
```

- [ ] **Step 2: Zkopírovat strukturu z `agnes-infra-keboola`, nahradit konkrétní hodnoty placeholdery**

```bash
# Zkopírovat strukturu
cp -r ../agnes-infra-keboola/terraform .
cp -r ../agnes-infra-keboola/.github .

# Reset konkrétní hodnoty
cat > terraform/main.tf <<'EOF'
terraform {
  required_version = ">= 1.5"
  required_providers {
    google = { source = "hashicorp/google", version = "~> 5.0" }
  }
  backend "gcs" {
    bucket = "REPLACE_WITH_YOUR_BUCKET"
    prefix = "REPLACE_WITH_CUSTOMER_NAME"
  }
}

provider "google" {
  project = var.gcp_project_id
  region  = var.region
  zone    = var.zone
}

module "agnes" {
  source = "github.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance?ref=infra-v1.1.0"

  gcp_project_id    = var.gcp_project_id
  region            = var.region
  zone              = var.zone
  customer_name     = var.customer_name
  seed_admin_email  = var.seed_admin_email
  data_source       = var.data_source
  keboola_stack_url = var.keboola_stack_url
  prod_instance     = var.prod_instance
  dev_instances     = var.dev_instances
}

output "prod_ip"      { value = module.agnes.prod_ip }
output "instance_ips" { value = module.agnes.instance_ips }
EOF

cat > terraform/variables.tf <<'EOF'
variable "gcp_project_id"    { type = string }
variable "region"            { type = string, default = "europe-west1" }
variable "zone"              { type = string, default = "europe-west1-b" }
variable "customer_name"     { type = string }
variable "seed_admin_email"  { type = string }
variable "data_source"       { type = string, default = "keboola" }
variable "keboola_stack_url" { type = string, default = "" }
variable "prod_instance"     { type = any }
variable "dev_instances"     { type = any, default = [] }
EOF

cat > terraform/terraform.tfvars.example <<'EOF'
# Kopie tohoto souboru → terraform.tfvars, vyplnit hodnoty.
# terraform.tfvars je gitignored (nikdy necommitovat!)

gcp_project_id    = "REPLACE"             # Váš GCP projekt
customer_name     = "REPLACE"             # Krátký identifikátor, např. "acme"
seed_admin_email  = "admin@example.com"
data_source       = "keboola"             # keboola | bigquery | csv
keboola_stack_url = "https://connection.keboola.com/"

prod_instance = {
  name         = "agnes-prod"
  machine_type = "e2-small"
  data_disk_gb = 50
  image_tag    = "stable"
  upgrade_mode = "auto"
  tls_mode     = "caddy"
  domain       = ""
}

dev_instances = [
  { name = "agnes-dev", image_tag = "dev" }
]
EOF
```

- [ ] **Step 3: Zkopírovat bootstrap skript z public repa**

```bash
cp ../tmp_oss/scripts/bootstrap-gcp.sh .
```

- [ ] **Step 4: Napsat README.md pro onboarding**

Write:

```markdown
# Agnes Infrastructure Template

Deploy Agnes (AI Data Analyst) into your own GCP project.

## Prerequisites

- GCP project with billing enabled
- `gcloud` CLI authenticated as project Owner
- `terraform` >= 1.5
- GitHub account (for private repo + Actions)

## 1. Bootstrap GCP

```bash
./bootstrap-gcp.sh <YOUR_GCP_PROJECT_ID>
```

Výstup: SA key JSON.

## 2. Klonovat template

```bash
gh repo create <YOUR_ORG>/agnes-infra --template keboola/agnes-infra-template --private
cd agnes-infra
```

## 3. Nastavit secrets

```bash
# SA key (z kroku 1)
gh secret set GCP_SA_KEY < path/to/key.json
rm path/to/key.json

# Keboola token (pokud data_source = keboola)
gcloud secrets create keboola-storage-token --data-file=- <<< "YOUR_TOKEN"
```

## 4. Konfigurace

Editovat `terraform/main.tf` — aktualizovat `backend.bucket` a `backend.prefix`.

Kopírovat `terraform/terraform.tfvars.example` → `terraform/terraform.tfvars`, vyplnit.

## 5. První apply

```bash
cd terraform
terraform init
terraform plan
terraform apply
```

IP prod VM je v outputu.

## 6. Login

```bash
# Bootstrap prvního admin usera
curl -X POST http://$(terraform output -raw prod_ip):8000/auth/bootstrap \
    -H "Content-Type: application/json" \
    -d '{"email": "YOU@example.com", "password": "YOUR_STRONG_PASSWORD"}'
```

Otevřít http://<prod_ip>:8000/login.

## 7. Upgrade workflow

- `:stable` image → auto-upgrade přes Watchtower
- Infra změna: PR v tomto repu → `terraform plan` v PR → merge → `apply` (prod vyžaduje reviewer)
- TF modul upgrade: Renovate otevře PR s novým `ref=infra-vX.Y.Z`

Další detaily: https://github.com/keboola/agnes-the-ai-analyst/blob/main/docs/ONBOARDING.md
```

- [ ] **Step 5: Vytvořit README + push + mark as template**

```bash
git add .
git commit -m "initial template"
git push -u origin main
gh repo edit keboola/agnes-infra-template --template
```

### Task 6.2: Napsat ONBOARDING.md v public repu

**Files:**
- Create: `docs/ONBOARDING.md` (v public repu)

- [ ] **Step 1: Napsat ONBOARDING.md**

Write `docs/ONBOARDING.md` obsah identický s README v template repu + poznámkou "fyzická šablona: keboola/agnes-infra-template".

- [ ] **Step 2: Commit**

```bash
cd "/Users/zdeneksrotyr/Library/Mobile Documents/com~apple~CloudDocs/Sources/VsCode/component_factory/tmp_oss"
git add docs/ONBOARDING.md
git commit -m "docs: onboarding guide for deploying Agnes per customer"
```

### Task 6.3: Vyzkoušet onboarding na dummy customer (sanity check)

- [ ] **Step 1: Vytvořit testovací GCP projekt**

```bash
gcloud projects create agnes-onboarding-test-$(date +%s) --name="Agnes onboarding test"
# Link billing (via UI) if required
```

- [ ] **Step 2: Spustit bootstrap**

```bash
./scripts/bootstrap-gcp.sh <test-project-id>
```

- [ ] **Step 3: Klonovat template do dummy repa**

```bash
gh repo create zdeneksrotyr/agnes-infra-test --template keboola/agnes-infra-template --private
gh repo clone zdeneksrotyr/agnes-infra-test
cd agnes-infra-test
```

- [ ] **Step 4: Projít README krok za krokem a změřit čas**

Cíl: end-to-end < 1 hod. Zaznamenat překážky, zpět do README.

- [ ] **Step 5: Cleanup — smazat test projekt**

```bash
gcloud projects delete <test-project-id>
gh repo delete zdeneksrotyr/agnes-infra-test --yes
```

### Task 6.4: Renovate configuration

- [ ] **Step 1: Přidat renovate.json do template repa**

Write `keboola/agnes-infra-template/renovate.json`:

```json
{
  "$schema": "https://docs.renovatebot.com/renovate-schema.json",
  "extends": ["config:base"],
  "customManagers": [
    {
      "customType": "regex",
      "fileMatch": ["\\.tf$"],
      "matchStrings": [
        "source\\s*=\\s*\"github\\.com/keboola/agnes-the-ai-analyst//infra/modules/customer-instance\\?ref=(?<currentValue>infra-v\\d+\\.\\d+\\.\\d+)\""
      ],
      "datasourceTemplate": "github-releases",
      "depNameTemplate": "keboola/agnes-the-ai-analyst",
      "packageNameTemplate": "keboola/agnes-the-ai-analyst",
      "versioningTemplate": "regex:^infra-v(?<major>\\d+)\\.(?<minor>\\d+)\\.(?<patch>\\d+)$"
    }
  ],
  "packageRules": [
    {
      "matchPackageNames": ["keboola/agnes-the-ai-analyst"],
      "matchUpdateTypes": ["major"],
      "prPriority": 10
    }
  ]
}
```

- [ ] **Step 2: Instalovat Renovate GitHub App na privátní repa**

Ruční krok v GitHub: Settings → Integrations → Renovate → grant access.

---

## Finální checkpoint

- [ ] **Fáze 1 complete** — prod běží z `:stable` image, žádný git pull z forku
- [ ] **Fáze 2 complete** — TF modul, PD, Keboola nasazena přes modul
- [ ] **Fáze 3 complete** — HTTPS funguje (pokud DNS dostupné)
- [ ] **Fáze 4 complete** — watchtower na dev VM auto-pulluje :dev, OS Login aktivní
- [ ] **Fáze 5 complete** — GHA CI/CD funguje, prod apply vyžaduje review
- [ ] **Fáze 6 complete** — template repo existuje, ONBOARDING.md, Renovate nakonfigurovaný
- [ ] **Starý osobní fork smazán**
- [ ] **Keboola token rotován a v Secret Manageru**
- [ ] **Dokumentace aktualizovaná**

---

## Self-Review

**Spec coverage:**
- §2 Model self-deploy → Task 1.2 (bootstrap), Task 2.3 (private repo), Task 6 (template) ✅
- §3 Repo architektura → Task 2.1 (modul), Task 6.1 (template), Task 2.3 (customer repo) ✅
- §4 Release model → Task 1.1 (per-branch tagging), existuje release.yml ✅
- §5 Branch-aware dev → Task 2.1 (dev_instances proměnná), Task 4.1 (watchtower) ✅
- §6 Prod upgrade model → Task 4.1 (auto via watchtower), pinned mode přes tfvars (zákazník zvolí) ✅
- §7 Security → Task 1.2-1.4 (Secret Manager, SA), Task 4.2 (OS Login), Task 5.2 (env protection) ✅
- §8 Onboarding → Task 6.1-6.4 ✅
- §9 Tok změn → Task 5.1-5.2 (plan/apply), Task 4.1 (watchtower pipeline) ✅
- §10 Backup/monitoring → částečně; monitoring je follow-up (§14) ✅

**Placeholder scan:** Všechny kódy, konfigurace, příkazy jsou konkrétní.

**Type consistency:** `prod_instance` object a `dev_instances` list mají konzistentní schéma napříč Task 2.1, Task 2.3, Task 6.1.

**Gap:** Zákazníkem-zvolený pinned upgrade režim (§6.1) spouští Renovate — Renovate konfigurace je v Task 6.4, ale nepokrývá upgrade image tagu (jen modul ref). Follow-up: rozšířit `customManagers` v renovate.json na `image_tag` v tfvars.
