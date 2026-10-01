PUMA — Cloud Run Deployment

IMPORTANT
- Do not deploy the stabilization branch over the production service.
- Use a separate TEST service until end-to-end validation is complete.
- Production workbook, Gmail labels, Drive folders, Cloud Run service and scheduler must remain unchanged during TEST validation.

TEST SERVICE
Recommended service name: puma-orchestrator-test
Region: us-central1
Scheduler timezone (when/if a TEST scheduler is added): America/New_York

Required Google services:
- run.googleapis.com
- cloudscheduler.googleapis.com
- secretmanager.googleapis.com
- cloudbuild.googleapis.com
- artifactregistry.googleapis.com
- logging.googleapis.com
- firestore.googleapis.com

TEST runtime environment:
PUMA_TEST_MODE=1
PUMA_SPREADSHEET_ID=1wjGB4dUTbBUiWVC7Kh0huVw8wM_ppmjTgktLw6ElVf4
PUMA_OKD_LABEL_ID=Label_1
PUMA_RFA_LABEL_NAME=PUMA TEST/RFA
PUMA_RFA_PROCESSED_LABEL_NAME=PUMA TEST/RFA Processed
PUMA_RFPO_LABEL_ID=Label_3
PUMA_PO_LABEL_NAME=PUMA TEST/PO
PUMA_RR_LABEL_NAME=PUMA TEST/RR
PUMA_RFPS_LABEL_ID=Label_6
PUMA_DR_LABEL_NAME=PUMA TEST/DR
PUMA_PO_DRIVE_FOLDER_ID=1EeexxItqTX896tq64lf1-8lBz2bqCBmH
PUMA_RR_BASE_FOLDER_ID=1WMpQwDwGGVq8R1AYxvJPAP8gOrgmP9Bg
PUMA_DR_BASE_FOLDER_ID=12hNy09LDDFAjyDl4q97GTTsCysyvSgJf
PUMA_ALLOWED_STEPS=OKD,RFA,RFPO,PO,RR,RFPS,DR
PUMA_DISABLED_STEPS=
PUMA_ARGS=--nodebug
PUMA_EXPECTED_GMAIL_ACCOUNT=adrian@afterimagelighting.com
PUMA_LEASE_SECS=900

Verified Gmail account:
adrian@afterimagelighting.com

Verified TEST Gmail label IDs:
- PUMA TEST/OKD = Label_1
- PUMA TEST/RFA = Label_2
- PUMA TEST/RFA Processed = Label_8
- PUMA TEST/RFPO = Label_3
- PUMA TEST/PO = Label_4
- PUMA TEST/RR = Label_5
- PUMA TEST/RFPS = Label_6
- PUMA TEST/DR = Label_7

Verified TEST Drive root:
1xkxl2Ep7qcGiWLcrNUB9xOY-5lXkYgvD

Verified TEST Drive destinations:
- PO PDFs = 1EeexxItqTX896tq64lf1-8lBz2bqCBmH
- Receiving Reports = 1WMpQwDwGGVq8R1AYxvJPAP8gOrgmP9Bg
- Delivery Reports = 12hNy09LDDFAjyDl4q97GTTsCysyvSgJf

SECRETS
Reuse existing Google/Gmail OAuth credentials only through Secret Manager / Cloud Run secret bindings.
Do not commit client secrets, refresh tokens, service-account keys or token.json contents to GitHub.

Expected secret-backed environment inputs used by cloud_entrypoint.py:
- OAUTH_CLIENT_JSON
- OAUTH_TOKEN_JSON

BUILD/DEPLOY TEMPLATE
Replace PROJECT_ID, PROJECT_NUMBER and SERVICE_ACCOUNT_EMAIL with the existing Google Cloud values.

0. Preflight:
   - Enable every Required Google service above, including firestore.googleapis.com.
   - Confirm the Artifact Registry Docker repository named "puma" exists in us-central1.
   - Confirm a Firestore database is initialized for PROJECT_ID. TEST mode fails closed if Firestore leasing is unavailable.
   - Use a dedicated Cloud Run service account for the TEST service.
   - The TEST service account needs, at minimum:
     * roles/datastore.user (Firestore lease)
     * roles/secretmanager.secretAccessor (OAuth secret bindings)
   - Do not grant the TEST service account production-specific resources that are not required.

1. Build:
   gcloud builds submit --tag us-central1-docker.pkg.dev/PROJECT_ID/puma/puma-orchestrator-test:latest

2. Prepare the TEST env file:
   cp PUMA_TEST_ENVIRONMENT.example puma-test.env

   Review puma-test.env before deploying. It must contain only the verified PUMA TEST workbook, labels and Drive destinations above. Do not add production IDs.

3. Deploy isolated TEST service:
   gcloud run deploy puma-orchestrator-test \
     --image us-central1-docker.pkg.dev/PROJECT_ID/puma/puma-orchestrator-test:latest \
     --region us-central1 \
     --platform managed \
     --service-account SERVICE_ACCOUNT_EMAIL \
     --no-allow-unauthenticated \
     --env-vars-file=puma-test.env \
     --set-secrets "OAUTH_CLIENT_JSON=projects/PROJECT_NUMBER/secrets/OAUTH_CLIENT_JSON:latest,OAUTH_TOKEN_JSON=projects/PROJECT_NUMBER/secrets/OAUTH_TOKEN_JSON:latest"

4. Confirm service URL:
   gcloud run services describe puma-orchestrator-test --region us-central1 --format="value(status.url)"

5. Validate /healthz first.

6. Before invoking /run, verify the TEST service account can access Firestore and the configured OAuth secrets. A lease error in TEST is a hard failure by design.

7. Invoke /run manually only after TEST-labeled fixture messages are prepared.

8. Inspect logs and verify all reads/writes reference only:
   - TEST workbook ID
   - PUMA TEST/* Gmail labels
   - TEST Drive destination IDs

9. Do not create or enable a production scheduler until the manual TEST replay is clean.

PRODUCTION RELEASE GATE
Production deployment is allowed only after:
- Apps Script TEST code is pushed to the copied bound TEST project.
- Apps Script read-only audits pass.
- TEST-only write cases pass.
- Cloud TEST replay passes.
- No production workbook/labels/folders are touched.
- PRs are reviewed and intentionally promoted from draft.
