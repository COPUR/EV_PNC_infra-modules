# EV_PNC packaging gate

Card M-02. The packaging rules of the repo set (Martin: ADP no cycles, SDP depend towards stability, SAP abstractness)
as one reusable workflow, `.github/workflows/packaging-check.yml`. Callers use the short file in
`templates/packaging-caller.yml`; the logic lives only here.

| Step | What it does | Fails when |
|---|---|---|
| Component graph | `packaging_check.py components.csv --strict` | a dependency cycle, an SDP violation, or D' > 0.5 |
| Declared dependencies | `change_rules.py deps` scans the repo's manifests (`build.gradle*`, `settings.gradle*`, `libs.versions.toml`, `pom.xml`, `go.mod`, `pyproject.toml`, `requirements*.txt`, `package.json`, `contracts.lock`, other `*.lock`) for `COPUR/EV_PNC_*` repositories, `com.evpnc:<component>` and `ev-pnc-<component>` packages | a cross-repo dependency is used but no component of the repo lists it in `components.csv` |
| Model rules (only with `model-repo: true`, only on pull requests) | `change_rules.py model` compares `schema/` and `tools/ocpi-spec-pin.json` with the PR's base | the schema or the spec pin changed and the version was not bumped enough, the two generated packages disagree on the version, `CHANGELOG.md` has no section for the new version, the section names no `ADR-NNNN`, or that ADR has no file in EV_PNC_docs `docs/adr` |

## `components.csv`

The component graph (33 components, 19 repos) lives here, in `packaging/components.csv`. It came from the repo plan
(`repo-plan/v2/components.csv`) and the checker output is unchanged from `repo-plan/v2/tools/packaging_check.py`.
Columns: `component, repo, kind, abstractness (estimate 0..1), depends_on (; separated)`. Infra, docs and specs-only repos
have no component and are skipped by the dependency rule.

**How a new cross-repo dependency goes in.** Callers pin this repository by commit. A pull request that adds a dependency
(1) adds it to `depends_on` here and merges, (2) moves the caller's pinned `auditor-ref` to that commit in the same
pull request that adds the dependency. The dependency and its `components.csv` line are then visible together in review,
and the gate fails any pull request that adds a dependency without the line.

`depends_on` may list more than the code uses today (a planned edge); the rule only fails on a dependency that is used and not declared.

## Model change rules

What counts as a bump, decided from the JSON schema diff (the rule in `CHANGELOG.md` of EV_PNC_ocpi-model, made mechanical):

| Change | Bump |
|---|---|
| a definition, property or enum value removed; a type, `$ref`, `format` or `pattern` changed; a property made required; `maxLength` lowered | major |
| a definition, property or enum value added; `maxLength` raised or lifted; a required property relaxed | minor |
| descriptions, `x-*` annotations only | patch |
| `tools/ocpi-spec-pin.json` moved | at least minor |

When the base has no version yet (first cut), no bump is required but the ADR rule still applies.

## Run it locally

```bash
python3 packaging/packaging_check.py packaging/components.csv --strict
python3 packaging/change_rules.py deps --components packaging/components.csv --repo EV_PNC_simulator --root ../EV_PNC_simulator
python3 packaging/change_rules.py model --base origin/main --root ../EV_PNC_ocpi-model --adr-dir ../EV_PNC_docs/docs/adr
python3 -m unittest discover -s packaging/tests -v
```

## Limits

- Abstractness is still a design estimate per component; replace it from code (jdeps, ArchUnit) later.
- Only the manifests listed above are read. A dependency reached some other way (a script that downloads a file) is not seen.
- The ADR check needs the ADR on EV_PNC_docs `main`; an ADR still in an open docs pull request fails the model rule until it merges.
- The schema diff is structural. It cannot see a meaning change that leaves the schema as it was (a description-only change is a patch).
- Rollout to the repositories (one caller file each) is a follow-up; this change adds the gate and runs it on this repository.
