# Reuse cleanup: decisions and when to revisit them

Context: a scan of 73 repos on 2026-11-26 found duplicated CI, community files and
CDK patterns. Most were consolidated into this repo's reusable workflows. This
records what was deliberately **not** done, with the evidence, so nobody redoes
the analysis.

## 1. Existing static-site stacks stay as they are (no move onto `specter-static-site`)

Method: synthesize each fork's stack and `specter_static_site.StaticSiteStack`
offline with identical inputs and compare resources by logical ID.

| Repo | Result |
|---|---|
| knit-n-stitch-website | 28 resources vs 12. Both S3 buckets would be replaced (the library always sets an explicit bucket name; the fork lets CloudFormation name them). DNS alias records, the alarm topic and three separate deployments differ. |
| scissors-website | Buckets are named after the domain; every core resource has a different logical ID; it has a budget alert pipeline, WAF web ACL and SPA-rewrite function the library lacks. |
| chilly-dog-website | Only `SiteBucket` and its policy share IDs; bucket name differs; distribution, certificate, headers policy and DNS records would be replaced. |

Decision: use the library for **new** stacks only.
Revisit when: the library gains a legacy-naming option (no explicit bucket name) and
configurable logical IDs for records and the alarm topic, or a stack is being
rebuilt anyway. Then re-run the comparison first.

## 2. `specter-pipeline` and `specter-observability`: new stacks only

`StandardPipeline` is built on CDK Pipelines (self-mutating, CodeStar connection).
access-analyzer-pipeline and cdk hand-build plain CodePipeline with custom
CodeBuild projects. Converting replaces live pipelines. Observability output is
stateless dashboards, so it carries no replacement risk, but each repo's dashboards
are custom. Revisit per repo if it is being rebuilt.

## 3. No `specter-cli-kit` library

The only code shared across the seven CLIs is `boto3.Session(profile_name=..., region_name=...)`
(aws-assume-cli, ssmtree, iamwhy), each wrapped in different error handling.
cdkdiff does not use boto3; stackdrift and secretsync have their own client and
backend modules. A library would hold almost nothing. The duplicated skeleton is
covered by the `python-cli-template` repo instead.
Revisit when: three or more CLIs share a real helper beyond constructing a session.

## 4. PyPI publishing keeps its `publish` job in each repo

PyPI Trusted Publishing checks the `job_workflow_ref` claim
(`warehouse/oidc/models/github.py`, `_check_job_workflow_ref`). In a reusable
workflow that claim names the reusable file, so a shared publish job can never
match a project's registered publisher. `python-build.yml` therefore does test and
build only; each caller keeps a small `publish` job.

## 5. CI check names

Adopting `python-ci.yml` renames a repo's check to `ci / Lint, Secrets & Test (py3.x)`.
Repos whose branch protection requires the old names (stackdrift, aws-assume-cli,
chilly-dog-website, ssmtree) were not migrated; the PRs were closed with branches kept.
To adopt: rename the required checks, then reopen.

## 6. Merging workflow-only changes runs push-to-main workflows

Merging a workflow-only change fired every `push: main` workflow in that repo,
including production Deploy (certificate-factory, claude-code-remote-environment,
scissors-website) and an ECR image push (yopass). Add `paths-ignore` for docs and
workflow-only changes to deploy triggers.
