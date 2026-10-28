# EV_PNC_infra-modules

Reusable Terraform modules, Helm library charts and reusable CI workflows for EV_PNC_ and EV_PnC_Consent_ services. No environment-specific values.

Part of the **EV_PNC** platform: a neutral eMSP integration layer for Turkey-first, Europe-ready Plug&Charge, navigation and payments. The platform is neither a CPO nor a payment institution.

> Plug in, charging starts, payment completes in the background.

## Scope

- Versioned modules: Kubernetes namespace and node pool, PostgreSQL HA, Redis, Kafka topics and ACLs, object storage with WORM lock, gateway with mTLS
- Helm library chart for services (probes, resources, network policy)
- Reusable CI workflows: build, test, contract test, secret scan, packaging check
- Mandatory cost tags: project, component, env, owner
- Consumed by EV_PNC_infra and EV_PnC_Consent_infra

## Tech stack

Terraform, Helm, GitHub Actions

## Packaging

- Published components: (no published component)
- Depends on: nothing outside this repo
- Depended on by: no repo
- Dependency rules: no cycles (ADP); depend towards more stable components (SDP); published `*-api` components hold abstractions, deployables hold concrete code (SAP). CI runs `tools/packaging_check.py --strict` from `EV_PNC_docs`.
- Owner: DevOps agent · Wave 1 (now, before the 2026-12-07 gate) · Visibility: public

## Proposed layout

```
terraform/modules/
helm/library/
.github/workflows/ (reusable)
```

## Related

- Plan: `repo-plan/v2/README.md` (project files); consent repos: `EV_PnC_Consent_*` (`repo-plan/consent/README.md`)
- Docs: [EV_PNC_docs](https://github.com/COPUR/EV_PNC_docs)

## Status

Scaffold only. Not production ready.

## License

Apache-2.0 (proposed)
