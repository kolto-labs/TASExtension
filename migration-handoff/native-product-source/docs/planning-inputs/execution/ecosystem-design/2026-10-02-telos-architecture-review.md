# Telos ecosystem architecture design receipt

This receipt records a documentation-only planning pass. The canonical product plan remains the sole execution authority. No runtime/source implementation, tests of production behavior, native build, rig operation or deployment occurred.

## Architecture overview

KPM bootstraps Telos Core. Core owns installation, profiles, addition lifecycle, admission and versioned service discovery. First-party TAS installs into Core and exposes its separately versioned supervisor/native/holorecord services. The standalone TAS distribution bundles Core without mandatory Lua or KotorPhasor. Telos Exchange is the working hub name; its catalog/discussion infrastructure is optional to offline execution.

## Change assessment

Updated STRATEGY positioning/users/boundaries/metrics/community track/brand. Added canonical KTD10–KTD14 and ecosystem architecture with component, lifecycle and transaction diagrams, manifest fields, provider API contracts, transactional profile installer, typed discovery graph, library/privacy semantics and publisher/community maintenance. Deepened M7 and U17; added U19–U23 and the ecosystem verification gate. Current evidence, runtime priorities and existing unit IDs remain intact.

Root's direct clarification names Telos Core and authorizes local design of Telos Exchange. Root verified HTTP 200 and the Kolto Labs title at https://koltolabs.bocloud.workers.dev/ with local React/Vite prerender/static Worker source in the named home workspace. The configured custom domain currently does not resolve. The proposed existing-site `/telos/` integration has separate Browse additions and Submit an addition actions. No public modification is claimed.

## Compliance check

Design respects dependency inversion: additions use a versioned facade and Core does not import TAS internals or catalog code. TAS retains execution/evidence ownership. Required package dependency cycles refuse; soft discovery relationships cannot force installation. No production import graph was changed or audited, so absence of existing code cycles is not claimed. API/schema/loader ABI versioning is separate, with strict unsupported-capability refusal and explicit migration. Native hooks, allowlisting, current pins and root-owned qualification retain their existing contract.

## Risk analysis

Native DLLs cannot be honestly described as sandboxed; failures may crash the game. Extension state can invalidate exact restore without qualification. Installation recovery depends on supported filesystem atomicity and must be fault-tested. Catalog trust/identity/compatibility are separate claims. A static Worker site alone supplies no authenticated backend or local installation authority. Sparse real community activity is unknown, not a reason to fabricate rankings. Planned files and APIs are proposed additions, not completed features.

## Recommendations and review state

Proceed with seek/reproducibility then The Routine, preserving the already-authorized critical path. Independent design contracts may progress locally, but implementation begins with the actual KPM inventory and public SDK/provider boundary. U20 must prove interrupted staging/commit/activation recovery before U21 shows real installation actions. U23 must demonstrate independent small consumers and version upgrades rather than a single large plugin. Root should run independent document review after integrating latest requirement IDs and source ledger coverage.

Inline checks: 23 unique U IDs, 14 unique KTD IDs, balanced Markdown fences and test-scenario/verification fields for every U19–U23. Review corrected TAS/Core coupling, clarified browser-to-local-manager installation authority and replaced the previously unknown website with root's verified target. Full ce-doc-review persona dispatch was not run in this worker lane; do not call this an independent review or implementation-ready production acceptance.

## Design sources

- Existing `docs/design/contract.md` coexistence/allowlisting and canonical plan runtime/preservation contracts.
- https://code.visualstudio.com/api/references/extension-manifest for identity, host compatibility and pack/dependency separation.
- https://code.visualstudio.com/api/references/activation-events for declared activation.
- https://theupdateframework.github.io/specification/latest/ for verified update metadata/expiry/rollback defense design basis.

## Written document hashes

- `STRATEGY.md` SHA-256 `a223d854530df44d355f1d570c5ef12b4507ce94af4c780e4f8fa0b6a5fe9e8e`
- `docs/plans/2026-10-02-2022-feat-kptas-complete-product-plan.md` SHA-256 `1dfe9b5b7fc827addcc1f33849640c0d851a7b9388d417a67faeffa68e9ed538`
