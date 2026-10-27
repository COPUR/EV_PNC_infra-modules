#!/usr/bin/env python3
"""Component-level package metrics (Martin: ADP, SDP, SAP) for the EV_PNC set.

Usage: python3 packaging_check.py components.csv [--strict]   (--strict exits 1 on a cycle, an SDP violation or D' > 0.5)

Input columns: component, repo, kind, abstractness (design estimate, 0..1), depends_on (; separated).
Prints: cycles (ADP), I, A, D' per component, SDP violations, and a per-repo roll-up.
A is an estimate until real counts exist; replace it from code (e.g. jdeps / ArchUnit) in CI.
I = Ce / (Ca + Ce); D' = |A + I - 1|. Zone of pain: A and I both low. Zone of uselessness: both high.

Origin: repo-plan v2 `tools/packaging_check.py` (card M-02 moved it here so the reusable workflow can run it);
the analysis is unchanged, it is only split into functions so the change rules and the tests can use it.
"""
from __future__ import annotations

import csv
import io
import sys
from collections import defaultdict
from dataclasses import dataclass, field


class ComponentsError(ValueError):
    pass


@dataclass
class Result:
    cycles: list[list[str]] = field(default_factory=list)
    sdp_violations: list[tuple[str, str]] = field(default_factory=list)
    metrics: dict[str, tuple[int, int, float, float, float]] = field(default_factory=dict)  # Ca, Ce, I, A, D'
    report: str = ""

    @property
    def failed(self) -> bool:
        return bool(self.cycles) or bool(self.sdp_violations) or any(m[4] > 0.5 for m in self.metrics.values())


def read_components(text: str) -> dict[str, dict]:
    rows = list(csv.DictReader(io.StringIO(text)))
    comp = {r["component"]: r for r in rows}
    if len(comp) != len(rows):
        raise ComponentsError("duplicate component names")
    return comp


def dependencies(comp: dict[str, dict]) -> dict[str, list[str]]:
    deps = {c: [d for d in r["depends_on"].split(";") if d] for c, r in comp.items()}
    missing = {d for ds in deps.values() for d in ds if d not in comp}
    if missing:
        raise ComponentsError(f"unknown components: {sorted(missing)}")
    return deps


def _cycles(comp: dict[str, dict], deps: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan strongly connected components of size > 1."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stack: list[str] = []
    on: set[str] = set()
    sccs: list[list[str]] = []
    counter = [0]
    sys.setrecursionlimit(10000)

    def strong(v: str) -> None:
        index[v] = low[v] = counter[0]
        counter[0] += 1
        stack.append(v)
        on.add(v)
        for w in deps[v]:
            if w not in index:
                strong(w)
                low[v] = min(low[v], low[w])
            elif w in on:
                low[v] = min(low[v], index[w])
        if low[v] == index[v]:
            scc = []
            while True:
                w = stack.pop()
                on.discard(w)
                scc.append(w)
                if w == v:
                    break
            if len(scc) > 1:
                sccs.append(sorted(scc))

    for v in comp:
        if v not in index:
            strong(v)
    return sccs


def analyze(text: str) -> Result:
    comp = read_components(text)
    deps = dependencies(comp)
    res = Result(cycles=_cycles(comp, deps))
    ca: dict[str, int] = defaultdict(int)
    for ds in deps.values():
        for d in ds:
            ca[d] += 1
    for c in comp:
        ce = len(deps[c])
        a_in = ca[c]
        i = ce / (a_in + ce) if (a_in + ce) else 0.0
        a = float(comp[c]["abstractness"])
        res.metrics[c] = (a_in, ce, i, a, abs(a + i - 1))
    res.sdp_violations = [(c, d) for c, ds in deps.items() for d in ds if res.metrics[d][2] > res.metrics[c][2] + 1e-9]

    out = [f"{len(comp)} components, {len({r['repo'] for r in comp.values()})} repos", "", "ADP (cycles): " + ("none" if not res.cycles else "")]
    out += [f"  CYCLE: {' <-> '.join(s)}" for s in res.cycles]
    out += ["", f"{'component':24}{'Ca':>3}{'Ce':>4}{'I':>6}{'A':>6}{'Dp':>6}  flag"]
    for c, (a_in, ce, i, a, d) in sorted(res.metrics.items(), key=lambda kv: kv[1][2]):
        flag = ""
        if d > 0.5 and a < 0.5 and i < 0.5:
            flag = "zone of pain"
        elif d > 0.5 and a >= 0.5 and i >= 0.5:
            flag = "zone of uselessness"
        elif d > 0.3:
            flag = "review"
        out.append(f"{c:24}{a_in:>3}{ce:>4}{i:>6.2f}{a:>6.2f}{d:>6.2f}  {flag}")
    out += ["", "SDP violations (a component depends on a less stable one, I(dep) > I(self)):"]
    out += ["  none"] if not res.sdp_violations else [f"  {c} (I={res.metrics[c][2]:.2f}) -> {d} (I={res.metrics[d][2]:.2f})" for c, d in res.sdp_violations]
    out += ["", "Repo roll-up (mean over components; I of a repo from its cross-repo edges):", f"{'repo':34}{'comps':>6}{'Ca':>4}{'Ce':>4}{'I':>6}"]
    repos: dict[str, list[str]] = defaultdict(list)
    for c, r in comp.items():
        repos[r["repo"]].append(c)
    for repo, cs in sorted(repos.items()):
        ce = len({d for c in cs for d in deps[c] if comp[d]["repo"] != repo})
        cain = len({c2 for c2 in comp for c in cs if c in deps[c2] and comp[c2]["repo"] != repo})
        i = ce / (ce + cain) if (ce + cain) else 0.0
        out.append(f"{repo:34}{len(cs):>6}{cain:>4}{ce:>4}{i:>6.2f}")
    res.report = "\n".join(out) + "\n"
    return res


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        res = analyze(open(args[0]).read())
    except (ComponentsError, OSError, KeyError) as e:
        print(f"packaging check: {e}", file=sys.stderr)
        return 2
    sys.stdout.write(res.report)
    if "--strict" in argv and res.failed:
        print("packaging check failed: cycle, SDP violation or D' > 0.5", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
