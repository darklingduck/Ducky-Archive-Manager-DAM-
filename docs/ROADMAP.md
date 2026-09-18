# DAM Roadmap

Ducky Archive Manager (DAM) is a safety-first personal information organization and cleanup application. It discovers, classifies, organizes, and accounts for information; deletion is a last, separately approved step. Gmail is its first real DataSource and proving ground, not the boundary of the application.

**The user remains in control of what happens to every DAM Item.** The user may change categories, classifications, and handling rules. Such changes must not silently rewrite historical provenance, reinterpret past human decisions as though they had always existed, authorize newly implied actions, or execute them. When a change affects existing items, DAM should determine the affected scope, explain the consequences, and obtain the authority required for any resulting actions.

`AGENTS.md` is authoritative for standing safety and design requirements. This roadmap tracks implementation state and direction; it does not replace that specification.

## Status key

- **COMPLETE** — implemented and committed.
- **PARTIAL** — a foundation exists, but the described workflow is incomplete.
- **CURRENT** — implementation actively underway.
- **PLANNED** — accepted direction, not implemented.
- **DEFERRED** — intentionally held for a later capability or safety gate.
- **INVESTIGATING** — a design decision remains open.
- **REJECTED** — an approach ruled out; retain its reason here.

No implementation step is currently marked CURRENT. Step 14B is the next proposed implementation step.

## Application architecture

The intended flow is:

Source Instance → DAM Item → rule/evidence evaluation → classification → Classification Queue when teaching is unresolved → classified item → handling-rule evaluation → Rules Queue when handling is unresolved → Action Plan → approval/authority → execution → verification → audit.

**Classification determines what information is. Handling rules determine what DAM may propose doing with it. Neither alone permits execution.**

Evidence confidence, human acceptance of classification knowledge, structured Review reasons, action planning, action authority, and actual execution are separate facts. Generic `Review=true` does not by itself mean category teaching is needed.

The CLI is an interface to DAM, not the owner of its workflow. Application/domain operations should also serve a future desktop or web-style GUI and, eventually, natural-language interaction. Interfaces may present choices differently; they must not independently define classification, queue membership, approvals, execution safety, or audit behavior.

## Completed implementation

| Step | Result | Commit |
| --- | --- | --- |
| 1–2 | Core models, safe configuration, and initial project foundation | `7a858c8` |
| 3 | Rule matching and evidence | `5425778` |
| 4 | Classification and precedence | `c5a8c47` |
| 5 | Non-executable action proposal safety | `9fc92ac` |
| 6 | Private SQLite persistence and historical records | `c7a2714` |
| 7 | Audit previews and statistics | `7a1a5f5` |
| 8 | Synthetic scan CLI | `f60bc6b` |
| 9 | Read-only Gmail metadata adapter | `052c59c` |
| 10 | Exact-scope read-only Gmail authentication boundary | `628d35b` |
| 11 | Explicit, Inbox-only real Gmail scan; maximum 10 individual messages | `f392b09` |
| 12 | Preview-first human classification learning | `dd7b4b5` |
| 12.1 | Explicit one-message Gmail review and learning bridge | `08eff4f` |
| 12.2 | Local category discovery | `1124b80` |
| 12.3 | Permanent `CAT-...` identity and private category management | `ff80a43` |
| 12.4 | Classification provenance and structured Review reasons | `8900bcb` |
| 12.5 | Clearer CLI hierarchy, confirmations, and Review presentation | `29b6e6a` |
| 13 | Source-neutral `SRC-...` / `ITEM-...` identity and durable synthetic Classification Queue | `48dd404` |
| 14A | Verified Gmail mailbox source binding through a read-only profile lookup | `1eff8cb` |

Today, synthetic scan is the default. Real Gmail scan requires explicit selection, reads Inbox metadata only, and is capped at 10 individual messages. Proposals are non-executable; there is no Gmail mutation or action-execution path. Step 14A deliberately stopped before real Gmail `ITEM-...` and Classification Queue intake.

## Operating model

**PLANNED:** Initial cleanup and ongoing maintenance are two situations served by the same application services and safety model, not separate engines. During initial cleanup, DAM should work incrementally through a historical backlog in bounded batches while classifications, handling rules, queues, and approved decisions are established. During ongoing maintenance, existing knowledge should help process new items. Items needing teaching, handling rules, Review, or approval should enter the appropriate durable workflow without blocking unrelated items.

**PLANNED:** Processing should be resumable. An interrupted run, application restart, or machine reboot should not lose already-persisted unresolved work or require rediscovering all prior work. Durable identities, observations, queue state, provenance, and completed state should support continuation from a known state. Detailed checkpoint mechanics remain for a later implementation step.

Interactive processing may offer **Teach now** or **Defer**. Unattended processing should persist unresolved work immediately and continue; one unknown item must not stop an otherwise valid batch. Future batch sizes and scheduling policies should be configurable. Multiple configured source instances may coexist, including multiple accounts from one provider. Sources may eventually run sequentially or under a controlled scheduler. Gmail remains the proving ground, not the application boundary.

Long-running operation must remain bounded and testable. Future background work needs deliberate resource lifetimes and stress/adversarial tests for memory growth, file handles, database cursors and connections, network sessions, subprocesses and threads, concurrency, caches and queues, and temporary resources. This is a requirement for future implementations, not a claim that unimplemented background processing is already leak-free.

## Next: Step 14B — Real Gmail durable classification workflow

**PLANNED.** Connect a verified Gmail `SRC-...` to exact Gmail message `ITEM-...` identities and durable Classification Queue work when category teaching is unresolved. The intended human workflow can offer **Teach now** or **Defer**; deferred work must remain available. Accepted learning should be usable by later items in the same active session, and queued members should be reevaluated individually. Independent Review reasons must remain intact.

This is a workflow connection, not permission to act on the mailbox. Step 14B does not itself raise the 10-message scan ceiling or introduce Gmail writes.

## Classification and handling work

### Classification Queue — “What is this?”

**PARTIAL.** Durable work identity, exact item membership, representative items, deferment, reevaluation, and immutable transition history exist for synthetic observations. A deferred item is pending human work, not forgotten. Uninspected items and generic Review alone do not create teaching work.

**PLANNED:** verified real-source intake and interface workflows. A work item may represent related items, but grouping is only a usability aid. There is no automatic grouping heuristic today; shared sender, provider, folder, or path does not prove shared classification. Every DAM Item remains individually identifiable and auditable. Teaching a representative item advances only members covered when each is reevaluated.

### Rules Queue — “What should happen to these?”

**PLANNED.** Classification must not imply one universal handling decision. One category may eventually have several handling rules for different streams or contexts. Unresolved handling work belongs in a separate queue, with exact item accountability and room for exceptions. Current rules and single ActionProposal provide a foundation, not a durable Rules Queue.

### Action plans, authority, and execution

**PLANNED.** Future plans may contain multiple explicit plan items, each with scope, rule provenance, approval requirements, state, and verification. Required/completed counts should be derived from records. Permanent action identity based on bit position is **REJECTED** because adding or rearranging actions would reinterpret history. Possible stable `ACT-...` identities require design when action planning is implemented; no ACT identity or durable action bitmask exists now.

Approval must bind the appropriate items, actions, rule/configuration versions, preview, source identity, and fresh source state. Classification acceptance never grants action authority. Future execution must distinguish proposed, approved, attempted, applied, failed, skipped, indeterminate, and verified outcomes where relevant. Category or rule changes must not silently execute newly implied actions or rewrite historical decisions.

## Sources, coverage, and progress

**PLANNED:** multiple simultaneous source instances, including additional Gmail accounts and later Outlook/Hotmail, Yahoo Mail, local files, OneDrive, Google Drive, Dropbox, and shared storage. Each configured account or source context needs its own stable `SRC-...`; one provider may have multiple instances.

`MessageMetadata` remains email-specific. Source-neutral `ITEM-...` identity wraps provider/type-specific observations; future files should not be forced into sender/subject fields. Categories describe what information is across sources, while available operations depend on the source.

**PLANNED:** per-source and combined inventory, coverage, and progress views, with useful historical snapshots or equivalent history. Source-reported totals are distinct from DAM-observed items. Progress may show changing inventory, increasing observation and classification coverage, Classification Queue and Rules Queue workload, and completed handling. No particular snapshot schedule, schema, chart, or interface is decided yet.

**100% accounted for does not mean 100% deleted.** A retained tax document, protected security message, archived receipt, or other item intentionally retained under an established rule is accounted for. Progress measures DAM’s understanding and completion of the user’s intended handling, not deletion volume.

Increasing the real Gmail read-only scan ceiling beyond 10 is **DEFERRED** until the durable workflow and its usability can be assessed. Scan expansion and interactive learning are separate decisions.

## Safety and security boundaries

DAM remains metadata-first. Fetch snippets or content only when needed, retrieve no attachments by default, do not automatically follow links or load remote content, and avoid unnecessary sensitive-content persistence. Credentials and OAuth tokens remain outside Git and outside source or queue identity.

### Untrusted content / no implicit execution

**Managed content is data, not DAM instructions or authority.** Ingesting, parsing, classifying, previewing, or organizing an item must not execute instructions or code found in it. This includes source code, shell commands, JavaScript, active HTML, macros, embedded scripts, executable attachments, malicious filenames, archive contents, URLs, AI-generated commands, and prompt-injection text.

Source content must not reach `eval()` or `exec()`, be imported for classification, be passed through a shell as a command, trigger macros or scripts, authorize link following, or become DAM instructions or approvals. Any future execution-capable component needs a separate, narrow trust boundary. These are standing requirements for future adapters and parsers, not a claim that those adapters already exist.

Gmail currently uses the exact `gmail.readonly` OAuth scope. Permanent Gmail deletion is prohibited. Trash is destructive and requires its own approved safety path; no Gmail writes are implemented today. Future effects require current dry-run scope, appropriate authority, protection and retention checks, bounded execution, reconciliation, verification, and durable audit. Human-approved classification rules do not authorize labeling, archive, priority changes, Trash, unsubscribe, file movement, or deletion.

## Deferred capabilities

- **Controlled mailbox actions:** label, archive, and priority handling need approval, freshness, idempotency, rollback planning, and verification before enabling writes. Trash is a separate later gate; permanent Gmail deletion is prohibited.
- **Unsubscribe:** a separate subsystem. Read-only discovery precedes a separately approved opt-out plan. Submission is not confirmation. Eligible historical cleanup is a separate exact-message action plan and approval; successful opt-out never authorizes sender-wide Trash. Unsupported mechanisms have no silent fallback.
- **Broader classification and organization:** provider-specific evidence, file classification, duplicate detection, retention, historical-account state, stronger confidence evidence, rule editing, and natural-language technical-rule proposals remain future work. Natural language and AI output cannot bypass safety or create authority.
- **Source reconciliation:** a changed Gmail profile address currently resolves as a different source identity. Merging identities or transferring historical items/work requires explicit future design.
- **GUI and scheduling:** use shared application operations after their workflow boundaries exist; neither is implemented.

## Roadmap maintenance

Update this file when a step is completed, a significant requirement is accepted, scope changes materially, an item is deferred, an approach is rejected, or a new step is inserted. Record the commit for each completed step. Retain rejected approaches with a short reason rather than removing their history. Trivial internal refactors do not require a roadmap entry. Keep `AGENTS.md` authoritative and keep complete, partial, planned, and deferred capabilities visibly distinct.
