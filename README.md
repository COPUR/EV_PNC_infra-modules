# EV_PNC_infra-modules

Reusable CI/CD building blocks for the EV_PNC and EV_PnC_Consent repositories. Callers pin a commit of this repository.

| Block | What | Docs |
|---|---|---|
| CI auditor and approver | gitleaks, repository and workflow hygiene, merge gate | [auditor/README.md](auditor/README.md) |
| Packaging gate | component graph check, declared cross-repo dependencies, model change rules (M-02) | [packaging/README.md](packaging/README.md) |
