# ShipMate AI — Azure Deploy Runbook (real, as-deployed)

> This is the **accurate** deploy reference, written from an actual end-to-end
> deploy on 2026-06-09. The older `DEPLOYMENT.md` is **stale** (describes an
> Azure-OpenAI/App-Service/read-only design that no longer matches the product)
> — follow THIS file, not that one.

---

## Live URLs

| Tier | URL |
|---|---|
| Frontend (Static Web App) | https://mango-sky-0deca8900.7.azurestaticapps.net |
| Backend (Container App) | https://shipmate-api.grayground-d22d56c1.southeastasia.azurecontainerapps.io |

Smoke checks: backend `/health` → `{"status":"healthy","agents":4}`; `/api/build/runs` → 200; `/api/analyze` with bad token → 401 (not 5xx).

---

## Azure account + subscription

- **Account:** `naman.229302638@muj.manipal.edu` ("Azure for Students")
- **Subscription id:** `bf159d50-fc3e-486e-b6c0-ff4bb095fbac`
- **Tenant:** `27282fdd-4c0b-4dfb-ba91-228cd83fdf71`
- **Re-auth (MFA expires often):** `az login --tenant 27282fdd-4c0b-4dfb-ba91-228cd83fdf71`
  — interactive (browser); a maintainer must do this. `az account show` can succeed
  from cache while real resource ops fail with `AADSTS50078 (MFA expired)`.

---

## Resources (resource group `shipmate-rg`)

| Resource | Type | Notes |
|---|---|---|
| `shipmate-api` | Container App | backend; FQDN above; env `shipmate-env` |
| `shipmate-env` | Container Apps managed environment | region southeastasia |
| `shipmate-web` | Static Web App (Free, **SwaCli** provider) | frontend; no GitHub-linked repo → deploy via SWA CLI + token |
| `shipmate-aoai` | Cognitive Services (Azure OpenAI) | southeastasia; see below |
| `workspace-shipmaterg…` | Log Analytics workspace | container logs |
| `builderfbb94` | `Microsoft.App/builders` | leftover managed buildpack builder from the original deploy |
| `namann`, `namann/Shipmate-AI` | Cognitive Services (eastasia) | **NAMAN'S — DO NOT TOUCH** |

---

## LLM provider on Azure

Bedrock (the local/default provider) does **not** work in the cloud (no AWS creds).
Set `SHIPMATE_LLM_PROVIDER=azure`. Real resource:

```
AZURE_OPENAI_ENDPOINT=https://shipmate-aoai.openai.azure.com/   # southeastasia
AZURE_OPENAI_API_VERSION=2024-10-21
AZURE_OPENAI_DEPLOYMENT_SMART=shipmate-smart                    # model gpt-4.1
AZURE_OPENAI_DEPLOYMENT_FAST=shipmate-fast                      # model gpt-4.1-mini
AZURE_OPENAI_API_KEY=<in Key Vault / app settings — NEVER commit>
```

Provider keys are read by `backend/app/services/azure_openai_provider.py`.

---

## ⚠️ Subscription constraints that break the "normal" deploy

The "Azure for Students" subscription blocks the usual Container App build path.
This is the single most important section.

1. **ACR Tasks are forbidden.** `az containerapp up --source` (and any ACR cloud
   build) fails with `TasksOperationsNotAllowed` — it tries to create an ACR and
   run a build task, which the sub disallows. **You cannot build the image in
   Azure.**
2. **No local Docker** on the dev machine in this setup → can't build locally either.
3. **The account lacks Entra/AD permissions to create a service principal**
   (`az ad sp create-for-rbac` → "Insufficient privileges"). So classic
   GitHub→Azure SP auth and OIDC app-registration are both unavailable.
4. **Region policy** "Allowed resource deployment regions": `malaysiawest,
   koreacentral, southeastasia, eastasia, austriaeast`.

**Conclusion:** build the image OFF Azure (GitHub Actions runner has Docker),
push to **GHCR**, and point the Container App at it. The only Azure-side action is
`az containerapp update --image …`, which works with a normal `az login`.

---

## Backend deploy (the working path)

### 1. Build + push the image (GitHub Actions → GHCR)

Workflow: `.github/workflows/deploy-backend.yml` (on `feat/azure-deploy`).
Builds `backend/Dockerfile` on an `ubuntu-latest` runner and pushes to
`ghcr.io/walkingdevflag/shipmate-api:{latest,<sha>}` using the built-in
`GITHUB_TOKEN` (needs `packages: write`). GHCR repo path **must be lowercase**
(owner `WalkingDevFlag` → `walkingdevflag`; the workflow downcases it).

Trigger: push to `feat/azure-deploy` (paths `backend/**`) or manually:
```bash
gh workflow run deploy-backend.yml --repo WalkingDevFlag/Shipmate-AI --ref feat/azure-deploy
gh run list --repo WalkingDevFlag/Shipmate-AI --workflow=deploy-backend.yml --limit 1
```

### 2. Give the Container App a GHCR pull credential

Image is **private**, so the app needs a credential. Use a GitHub PAT with
**`read:packages`** scope (classic token; the `gh` CLI's own token does NOT have
this scope):
```bash
az containerapp registry set -n shipmate-api -g shipmate-rg \
  --server ghcr.io --username WalkingDevFlag --password <PAT-with-read:packages>
```

### 3. Point the Container App at the new image

```bash
az containerapp update -n shipmate-api -g shipmate-rg \
  --image ghcr.io/walkingdevflag/shipmate-api:<sha>
```

### 4. ⚠️ GOTCHA — fix the entrypoint (buildpack remnant)

The Container App's container config still carries the **original buildpack
entrypoint** (`command: ["/cnb/lifecycle/launcher"]` + buildpack args). Our
Dockerfile image has no `/cnb/lifecycle/launcher`, so the new revision crashes:

```
exec: "/cnb/lifecycle/launcher": stat /cnb/lifecycle/launcher: no such file or directory
→ revision ActivationFailed / Unhealthy
```

Fix: override command+args to run uvicorn via a shell. The `az ... --args` flag
mis-parses leading-dash tokens, so do it via **YAML** (multi-element arrays):

```yaml
# in the container spec:
command: ['/bin/sh']
args: ['-c', 'uvicorn app.main:app --host 0.0.0.0 --port 8000']
```
```bash
# export, STRIP the leading "WARNING:" line (az -o yaml prepends stderr!),
# edit command/args, re-apply:
az containerapp show -n shipmate-api -g shipmate-rg -o yaml > ca.yaml
#   …remove line 1 if it's the "behavior altered by extension" WARNING…
#   …set command/args as above…
az containerapp update -n shipmate-api -g shipmate-rg --yaml ca.yaml
```
After this the image's uvicorn starts and `/health` + the new routes respond.

---

## Frontend deploy (Static Web App)

`shipmate-web` is a **Free-tier SwaCli** app (no linked repo). Deploy the built
`dist/` with the SWA CLI + deployment token.

**API base:** Free SWA can't reliably proxy `/api/*` to a Container App (linked
backends need Standard tier), so build the frontend pointing **directly** at the
backend URL. The backend's CORS allowlist already includes the SWA origin.

```bash
cd frontend
VITE_API_BASE="https://shipmate-api.grayground-d22d56c1.southeastasia.azurecontainerapps.io/api" \
  npm run build

TOKEN=$(az staticwebapp secrets list -n shipmate-web -g shipmate-rg --query "properties.apiKey" -o tsv)

# NOTE: the dev machine's npm registry points at a private Amazon CodeArtifact
# (token expires → E401). Force the PUBLIC registry for the SWA CLI:
npx --yes --registry=https://registry.npmjs.org/ @azure/static-web-apps-cli@latest \
  deploy ./dist --deployment-token "$TOKEN" --env production
```

Verify: `curl -s https://mango-sky-0deca8900.7.azurestaticapps.net/ | grep index-` shows the freshly-built bundle hash.

---

## Wiring (already set on `shipmate-api` env)

```
SHIPMATE_LLM_PROVIDER=azure
AZURE_OPENAI_* (see above)
ENVIRONMENT=production                 # disables /docs,/redoc,/openapi.json
ALLOWED_ORIGINS=https://mango-sky-0deca8900.7.azurestaticapps.net,http://localhost:5173,http://127.0.0.1:5173
GITHUB_REDIRECT_URI=https://mango-sky-0deca8900.7.azurestaticapps.net/github/callback
FRONTEND_URL=https://mango-sky-0deca8900.7.azurestaticapps.net
GITHUB_CLIENT_ID / GITHUB_CLIENT_SECRET
SHIPMATE_PYTEST_GATE=0                  # off in the hosted backend
```

OAuth: the GitHub OAuth App's callback must equal `GITHUB_REDIRECT_URI` above.

---

## Security TODOs (known, not yet done)

- **Rotate the GHCR PAT** used for `registry set` (it was pasted in chat). The app
  keeps its own stored copy, so rotating won't break the running app — only the
  next image-pull-cred update would need a fresh token.
- **Move secrets to Key Vault.** `AZURE_OPENAI_API_KEY` and `GITHUB_CLIENT_SECRET`
  are currently plaintext env vars on the Container App; migrate to Key Vault
  references + a managed identity.
- **Durable storage.** `SHIPMATE_STORE_DIR` defaults to `/tmp` (wiped on restart) —
  the session vault lives there. Mount a durable, ideally encrypted volume for a
  real multi-user deploy.

---

## One-shot redeploy cheat-sheet

```bash
az login --tenant 27282fdd-4c0b-4dfb-ba91-228cd83fdf71            # if MFA expired
gh workflow run deploy-backend.yml --repo WalkingDevFlag/Shipmate-AI --ref feat/azure-deploy
# wait for green, grab the sha:
SHA=$(gh run list --repo WalkingDevFlag/Shipmate-AI --workflow=deploy-backend.yml --limit 1 --json headSha -q '.[0].headSha')
az containerapp update -n shipmate-api -g shipmate-rg --image ghcr.io/walkingdevflag/shipmate-api:$SHA
# entrypoint override persists across image updates, so step 4 is one-time.
# frontend:
cd frontend && VITE_API_BASE="https://shipmate-api.grayground-d22d56c1.southeastasia.azurecontainerapps.io/api" npm run build
npx --yes --registry=https://registry.npmjs.org/ @azure/static-web-apps-cli@latest deploy ./dist \
  --deployment-token "$(az staticwebapp secrets list -n shipmate-web -g shipmate-rg --query properties.apiKey -o tsv)" --env production
```
