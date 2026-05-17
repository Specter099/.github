# Static Site CI/CD — Reusable Workflows Plan

**Goal:** Add two new reusable workflows (`static-site-review` and `static-site-deploy`) to this
repo that handle the React/Vite + CDK Python pattern, then wire `scissors-website` to use them.

**Architecture:** The scissors-website repo has a split layout — frontend in
`scissors-hair-barber/` and CDK infra in `infra/`. The reusable workflows accept `frontend-dir`
and `infra-dir` inputs so they work for any repo using this pattern. The existing `setup-cdk`
composite action needs a `requirements-path` input so it can install pip deps from a
subdirectory.

**Tech Stack:** Node 20, React/Vite, Vitest, ESLint, Python 3.12, AWS CDK 2.1106.1 CLI,
pytest, pip-audit, GitHub Actions reusable workflows.

---

## Repos involved

| Repo | Changes |
|------|---------|
| `Specter099/.github` (this repo) | Extend `setup-cdk`; add `static-site-review.yml` and `static-site-deploy.yml` |
| `Specter099/scissors-website` | Replace misplaced workflow with thin callers; update dev requirements; configure secrets |

---

## Task 1 — Extend `setup-cdk` with `requirements-path` input

**File:** `.github/actions/setup-cdk/action.yml`

The action currently hardcodes `pip install -r requirements.txt`. scissors-website keeps its
Python deps at `infra/requirements.txt`. Add a `requirements-path` input (default:
`requirements.txt`) so the same action works for both layouts without breaking existing callers.

Add to `inputs:`:
```yaml
  requirements-path:
    description: Path to requirements.txt relative to repo root
    required: false
    default: "requirements.txt"
```

Change the install step:
```yaml
    - name: Install Python dependencies
      shell: bash
      run: pip install -r ${{ inputs.requirements-path }}
```

Commit: `feat: add requirements-path input to setup-cdk action`

---

## Task 2 — Create `static-site-review.yml`

**File:** `.github/workflows/static-site-review.yml`

Runs on PRs. Lints and tests the frontend, runs CDK infra unit tests, audits Python deps,
then runs CDK synth/diff and posts the diff as a PR comment.

```yaml
name: Static Site Review

on:
  workflow_call:
    inputs:
      frontend-dir:
        description: Path to the frontend project directory (contains package.json)
        type: string
        required: true
      infra-dir:
        description: Path to the CDK infra directory (contains app.py)
        type: string
        required: true
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
      cdk-version:
        description: CDK CLI npm version
        type: string
        default: "2.1106.1"
      smoke-test-url:
        description: Unused in review — accepted for interface consistency with deploy workflow
        type: string
        default: ""

jobs:
  review:
    name: Lint, Test & Diff
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    steps:
      - uses: actions/checkout@v4

      - name: Setup CDK
        uses: Specter099/.github/.github/actions/setup-cdk@main
        with:
          cdk-version: ${{ inputs.cdk-version }}
          requirements-path: ${{ inputs.infra-dir }}/requirements.txt

      - name: Install frontend dependencies
        run: npm ci --prefix ${{ inputs.frontend-dir }}

      - name: Lint frontend (ESLint)
        run: npm run lint --prefix ${{ inputs.frontend-dir }}

      - name: Test frontend (Vitest)
        run: npm run test --prefix ${{ inputs.frontend-dir }}

      - name: Install infra dev dependencies
        run: pip install -r ${{ inputs.infra-dir }}/requirements-dev.txt

      - name: Test infra (pytest)
        run: pytest ${{ inputs.infra-dir }}/tests/ -v

      - name: Dependency audit
        run: pip-audit -r ${{ inputs.infra-dir }}/requirements.txt

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ inputs.aws-region }}

      - name: CDK Synth
        working-directory: ${{ inputs.infra-dir }}
        run: cdk synth

      - name: CDK Diff
        id: diff
        working-directory: ${{ inputs.infra-dir }}
        run: |
          diff_output=$(cdk diff 2>&1) || true
          echo "$diff_output"
          {
            echo "diff<<EOF"
            echo "$diff_output"
            echo "EOF"
          } >> "$GITHUB_OUTPUT"

      - name: Comment diff on PR
        uses: actions/github-script@v7
        env:
          DIFF: ${{ steps.diff.outputs.diff }}
        with:
          script: |
            const diff = process.env.DIFF;
            const body = `### CDK Diff\n\n\`\`\`\n${diff}\n\`\`\``;
            const { data: comments } = await github.rest.issues.listComments({
              owner: context.repo.owner,
              repo: context.repo.repo,
              issue_number: context.issue.number,
            });
            const existing = comments.find(c =>
              c.user.type === 'Bot' && c.body.startsWith('### CDK Diff')
            );
            if (existing) {
              await github.rest.issues.updateComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                comment_id: existing.id,
                body,
              });
            } else {
              await github.rest.issues.createComment({
                owner: context.repo.owner,
                repo: context.repo.repo,
                issue_number: context.issue.number,
                body,
              });
            }
```

Commit: `feat: add static-site-review reusable workflow`

---

## Task 3 — Create `static-site-deploy.yml`

**File:** `.github/workflows/static-site-deploy.yml`

Runs on push to main. Builds the frontend, deploys via CDK, uploads stack outputs as an
artifact, writes them to the job summary, and optionally runs a smoke test.

```yaml
name: Static Site Deploy

on:
  workflow_call:
    inputs:
      frontend-dir:
        description: Path to the frontend project directory (contains package.json)
        type: string
        required: true
      infra-dir:
        description: Path to the CDK infra directory (contains app.py)
        type: string
        required: true
      aws-region:
        description: AWS region
        type: string
        default: "us-east-1"
      cdk-version:
        description: CDK CLI npm version
        type: string
        default: "2.1106.1"
      smoke-test-url:
        description: URL to curl after deploy for smoke test (optional)
        type: string
        default: ""

jobs:
  deploy:
    name: Build & Deploy
    runs-on: ubuntu-latest
    permissions:
      id-token: write
      contents: read
    environment: production
    steps:
      - uses: actions/checkout@v4

      - name: Setup CDK
        uses: Specter099/.github/.github/actions/setup-cdk@main
        with:
          cdk-version: ${{ inputs.cdk-version }}
          requirements-path: ${{ inputs.infra-dir }}/requirements.txt

      - name: Install frontend dependencies
        run: npm ci --prefix ${{ inputs.frontend-dir }}

      - name: Build frontend
        run: npm run build --prefix ${{ inputs.frontend-dir }}

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_ROLE_ARN }}
          aws-region: ${{ inputs.aws-region }}

      - name: CDK Deploy
        working-directory: ${{ inputs.infra-dir }}
        run: cdk deploy --require-approval never --outputs-file outputs.json

      - name: Upload stack outputs
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: stack-outputs
          path: ${{ inputs.infra-dir }}/outputs.json
          if-no-files-found: ignore

      - name: Write outputs to job summary
        if: always()
        run: |
          if [ -f "${{ inputs.infra-dir }}/outputs.json" ]; then
            echo "## Stack Outputs" >> "$GITHUB_STEP_SUMMARY"
            echo '```json' >> "$GITHUB_STEP_SUMMARY"
            cat "${{ inputs.infra-dir }}/outputs.json" >> "$GITHUB_STEP_SUMMARY"
            echo '```' >> "$GITHUB_STEP_SUMMARY"
          else
            echo "No stack outputs file found." >> "$GITHUB_STEP_SUMMARY"
          fi

      - name: Smoke test
        if: ${{ inputs.smoke-test-url != '' }}
        run: |
          status=$(curl -o /dev/null -s -w "%{http_code}" --max-time 15 \
            "${{ inputs.smoke-test-url }}" || echo "000")
          echo "Smoke test HTTP status: $status"
          echo "## Smoke Test" >> "$GITHUB_STEP_SUMMARY"
          echo "Response from \`${{ inputs.smoke-test-url }}\`: **HTTP $status**" >> "$GITHUB_STEP_SUMMARY"
          if [[ "$status" == "000" ]]; then
            echo "::warning::Smoke test: curl failed or timed out"
          else
            echo "Smoke test passed — endpoint is reachable."
          fi
```

Commit: `feat: add static-site-deploy reusable workflow`

---

## Task 4 — Create caller workflows in scissors-website

**Files:**
- Create: `.github/workflows/review.yml`
- Create: `.github/workflows/deploy.yml`
- Delete: `.github/deploy.yml` (wrong location — never triggered by GitHub Actions)

> The existing `.github/deploy.yml` is at the wrong path. GitHub Actions only reads from
> `.github/workflows/`. That file is not running. The `workflows/` subdirectory does not
> currently exist in the repo.

`.github/workflows/review.yml`:
```yaml
name: Review

on:
  pull_request:
    branches: [main]

jobs:
  review:
    uses: Specter099/.github/.github/workflows/static-site-review.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
      pull-requests: write
    with:
      frontend-dir: scissors-hair-barber
      infra-dir: infra
      smoke-test-url: "https://scissorshairandbarber.com"
```

`.github/workflows/deploy.yml`:
```yaml
name: Deploy

on:
  push:
    branches: [main]
  workflow_dispatch:

concurrency:
  group: deploy
  cancel-in-progress: false

jobs:
  deploy:
    uses: Specter099/.github/.github/workflows/static-site-deploy.yml@main
    secrets: inherit
    permissions:
      id-token: write
      contents: read
    with:
      frontend-dir: scissors-hair-barber
      infra-dir: infra
      smoke-test-url: "https://scissorshairandbarber.com"
```

Commit: `ci: replace misplaced workflow with reusable review + deploy workflows`

---

## Task 5 — Update `infra/requirements-dev.txt` in scissors-website

**File:** `infra/requirements-dev.txt`

Current contents: `pytest==6.2.5` (only). The review workflow runs `pip-audit`, which must
be installed. Update to:

```
-r requirements.txt
pytest>=8.0.0
pip-audit>=2.8.0
```

Commit: `chore: add pip-audit to infra dev requirements, update pytest pin`

---

## Task 6 — Configure GitHub for scissors-website (one-time setup)

These are manual steps, not file changes.

**Set `AWS_ROLE_ARN` secret:**

The existing workflow constructs the ARN inline from `secrets.AWS_ACCOUNT_ID`. The reusable
workflows expect `secrets.AWS_ROLE_ARN` (full ARN). Set it:

```bash
gh secret set AWS_ROLE_ARN \
  --repo Specter099/scissors-website \
  --body "arn:aws:iam::<account-id>:role/GitHubActionsRole-Scissors"
```

Find the account ID: `aws sts get-caller-identity --query Account --output text`

**Check the IAM role trust policy:**

The `sub` condition must use a wildcard to allow PR branch workflows (not just main):

```json
"StringLike": {
  "token.actions.githubusercontent.com:sub": "repo:Specter099/scissors-website:*"
}
```

Check: `aws iam get-role --role-name GitHubActionsRole-Scissors --query Role.AssumeRolePolicyDocument`

If pinned to `ref:refs/heads/main`, update it to `*`.

**Create `production` environment:**

Go to **github.com/Specter099/scissors-website → Settings → Environments → New environment**
and create `production`. Required by the deploy workflow; without it the job queues indefinitely.

---

## Checklist

- [ ] Task 1: Extend `setup-cdk` with `requirements-path` input
- [ ] Task 2: Create `static-site-review.yml` in this repo
- [ ] Task 3: Create `static-site-deploy.yml` in this repo
- [ ] Task 4: Create caller workflows in `scissors-website`
- [ ] Task 5: Update `infra/requirements-dev.txt` in `scissors-website`
- [ ] Task 6: Set `AWS_ROLE_ARN` secret, fix trust policy, create `production` environment
- [ ] Open a PR on `scissors-website` → confirm `Review / Lint, Test & Diff` passes
- [ ] Merge → confirm `Deploy / Build & Deploy` passes
