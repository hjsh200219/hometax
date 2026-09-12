# Harness Maturity Framework

Use this as a lightweight review aid for documentation and agent readiness.

## Levels

- L1: Entry points are missing or stale.
- L2: Entry points exist but duplicate behavior or omit quality evidence.
- L3: Root map, architecture map, quality notes, and risk tracker are present and mostly accurate.
- L4: Docs are routinely updated when behavior changes, with tests and risks linked.
- L5: Harness docs stay small, accurate, and actively prevent unsafe live-service mistakes.

## Dimensions

- Navigation: can an agent find the right source quickly?
- Correctness: do docs match actual code and tests?
- Safety: do docs preserve HomeTax credential and write-flow boundaries?
- Maintenance: can stale sections be found and removed without ceremony?

Current docs-only setup targets L3.

For the GC numerical snapshot, use the P1–P12 evidence scores:
`A=(P1+P2+P5+P12)/4`, `B=(P3+P4+P10)/3`, `C=(P6+P9+P11)/3`,
`D=(P7+P8)/2`; total = `(A*0.3+B*0.3+C*0.2+D*0.2)*10`.
This is a maintenance snapshot, not proof of production safety.
