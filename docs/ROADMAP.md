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

Steps 14B and 14C are complete, and Step 14C live acceptance passed. Architecture review is complete; preparatory operation/presentation migrations now cover `teach list`, `teach show`, `teach status`, and `teach preview`.

## Application architecture

The intended flow is:

Source Instance → DAM Item → rule/evidence evaluation → classification → Classification Queue when teaching is unresolved → classified item → handling-rule evaluation → Rules Queue when handling is unresolved → Action Plan → approval/authority → execution → verification → audit.

**Classification determines what information is. Handling rules determine what DAM may propose doing with it. Neither alone permits execution.**

Evidence confidence, human acceptance of classification knowledge, structured Review reasons, action planning, action authority, and actual execution are separate facts. Generic `Review=true` does not by itself mean category teaching is needed.

**DAM does not overwrite provenance-sensitive decisions merely because a newer decision exists.** A new classification evaluation is appended and linked to what it reevaluates or supersedes. This audit principle may later inform other domains without forcing them into one generic model.

The CLI is an interface to DAM, not the owner of its workflow. Application/domain operations should also serve a future desktop or web-style GUI and, eventually, natural-language interaction. Interfaces may present choices differently; they must not independently define classification, queue membership, approvals, execution safety, or audit behavior.

## Accepted Orchestrator direction — PLANNED

**PLANNED:** The Orchestrator will be DAM's sole execution controller. Every application invocation, from CLI or a future interface, enters and remains under its control. Continuous and direct/single-operation execution are modes/scopes of this one architecture. The Orchestrator determines the mode and requested scope, invokes the appropriate service, receives its typed result, and decides whether to continue, redirect, resume, or stop. Direct commands request one operation through the controller and then stop; no CLI-to-service bypass path is allowed. It owns session lifecycle, stage routing, interruption, and eventual resume; services own their domain operations. A stable Session ID correlates context but grants no domain-data authority. The Orchestrator holds only workflow-control state; functions load their own work through constrained interfaces and do not call sibling stages or the Orchestrator to advance a path.

**PARTIAL:** The first teaching-query slice has explicit private display contracts and a stateless Writer. The broader Writer/Presenter boundary remains planned: it will render explicitly supplied, sensitivity-aware presentation data under the application flow; it cannot control transitions or query arbitrary data via Session ID. A corresponding input boundary will avoid permanently coupling application functions to terminal prompts. Shared components need explicit state ownership and lifetimes; future adversarial tests should probe cross-function, ITEM, interaction, and session isolation with distinctive sentinel data. Session persistence, exact interfaces, and storage design remain undecided.

**PLANNED:** Continuous operation should finish a bounded scan under one effective configuration, then offer unresolved Classification Queue work through Teach or Defer without requiring separate command invocations. Only implemented, permitted stages run; user choices may interrupt or change the path. Existing `dam scan` and `dam teach` commands remain useful direct operations. Current CLI dispatch and scan/application composition predate this Orchestrator and must be assessed before implementation; no Orchestrator or new entry-point command exists yet.

## Complete: First operation/presentation migration slice

**COMPLETE — commit subject `Add DAM operation presentation boundaries` (this change; 713 tests passed, including 31 new focused tests).** Only `dam teach list`, `dam teach show`, and `dam teach status` use the new query boundary. CLI syntax becomes an explicit `TeachingQuery` and basic target/path inputs. `query_teaching` owns its Storage lifetime and returns an immutable `QueryControlResult` containing only a fixed execution disposition. Successfully querying pending Teaching work is a completed query, not a failed or pending query.

A separate `TeachingPresentationSink` receives one of three immutable display releases: `TeachingQueueListing`, `TeachingWorkDetail`, or `TeachingStatus`. Listing rows are immutable too. Contracts reject broad/mutable payload values; they contain only deliberately displayed fields, with stored From/Subject sanitized before release. List/status omit those fields. `render_teaching` is stateless, rejects unsupported payload types, and has no domain, configuration, storage, or source-query dependencies. The terminal adapter prints its output; control results contain no presentation data. Private display contracts are for requested display only, without logging, caching, or history. This does not implement temporary-content transport or guarantee erasure of terminal output.

Existing successful output and expected-error stream/exit behavior are preserved. Unexpected query/setup defects produce a fixed internal-error message without exception content. Tests cover exact output, absent/empty/redacted/truncated metadata, source exclusion, no query writes on current storage, control minimization, immutable contracts, cross-item/operation/synthetic-scope disclosure, and payload release after success, failure, and interruption. The safe metadata display helper moved without changing behavior; existing review callers retain its alias.

**Accepted vocabulary:** an Operation is the bounded executable application responsibility; a Stage is its contextual position in future Orchestrator-controlled workflow, not a separate class hierarchy. A Primitive is a reusable computation or narrow capability within that responsibility and cannot select application workflow. Existing Scan/Teaching classification, rule matching, and non-executable proposal composition remains valid.

This is a preparatory seam, not universal orchestration. CLI dispatch still controls invocation. All other command paths retain their earlier architecture. No Orchestrator, continuous interaction, Input Provider, Session ID/persistence, or temporary expanded evidence exists. Teaching save/recovery and queue-wide independent reevaluation are unchanged. `Storage.open` retains setup/migration behavior; no schema change or new read-only opening mode was added. Gmail behavior and action authority are unchanged. Further command migrations and the sole-controller entry point remain future work.

## Complete: Teaching preview boundary

**COMPLETE — commit subject `Add DAM teaching preview boundary` (this change; 744 tests passed, including 31 new focused tests).** `dam teach preview` now supplies explicit work/category/optional ITEM inputs and original file-override strings to `preview_teaching`. The Operation owns its Storage lifetime, uses the unchanged `TeachingService.preview` computation, and returns the existing immutable disposition-only `QueryControlResult`. It separately releases a `TeachingPreviewDisplay` through the existing `TeachingPresentationSink` after storage closes. The contract contains only authorized scalar display/confirmation values; no metadata, observation, candidate rule, repository row, or exception object escapes to the CLI or Writer.

The stateless terminal renderer formats the existing output and uses `shlex.join` on structured confirmation fields. Original category selectors, explicitly selected ITEMs, and file-override spelling are preserved. A GUI can consume those fields without parsing or displaying shell syntax. The Writer neither computes the fingerprint nor infers additional scope. Fingerprint computation and binding remain unchanged in Teaching: a later save must validate the exact proposal again. A displayed fingerprint grants no general, Gmail, or session authority and never invokes save automatically.

Compatibility/isolation tests cover exact output and command quoting, key/permanent category selectors, representative/explicit ITEM behavior, deterministic fingerprint binding and changes to scope/evidence/configuration, rejection of mismatched confirmation, safe sender display, minimal control, no source access, no preview query writes, and payload release across items, errors, and interruption. Existing `Storage.open` setup/migration behavior remains unchanged. No schema or Gmail change was made. `teach save` and `teach resume` retain their prior CLI architecture and persistence/recovery behavior. No Orchestrator, continuous interaction, Session ID, or temporary evidence feature was introduced.

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
| 14B | Explicit durable Gmail classification intake and conditional Classification Queue work; 669 tests passed | `2556280` |
| 14C | Offline interactive teaching, immutable linked classification evaluations, and recoverable exact-ITEM reevaluation; 682 tests passed | `9a9d08b` |

Today, synthetic scan is the default. Real Gmail inspection requires explicit selection; durable local intake additionally requires `--record-locally`. Gmail reads Inbox metadata only and is capped at 10 individual messages. Proposals are non-executable; there is no Gmail mutation or action-execution path.

## Operating model

**PLANNED:** Initial cleanup and ongoing maintenance are two situations served by the same application services and safety model, not separate engines. During initial cleanup, DAM should work incrementally through a historical backlog in bounded batches while classifications, handling rules, queues, and approved decisions are established. During ongoing maintenance, existing knowledge should help process new items. Items needing teaching, handling rules, Review, or approval should enter the appropriate durable workflow without blocking unrelated items.

**PLANNED:** Processing should be resumable. An interrupted run, application restart, or machine reboot should not lose already-persisted unresolved work or require rediscovering all prior work. Durable identities, observations, queue state, provenance, and completed state should support continuation from a known state. Detailed checkpoint mechanics remain for a later implementation step.

Interactive processing may offer **Teach now** or **Defer**. Unattended processing should persist unresolved work immediately and continue; one unknown item must not stop an otherwise valid batch. Future batch sizes and scheduling policies should be configurable. Multiple configured source instances may coexist, including multiple accounts from one provider. Sources may eventually run sequentially or under a controlled scheduler. Gmail remains the proving ground, not the application boundary.

Long-running operation must remain bounded and testable. Future background work needs deliberate resource lifetimes and stress/adversarial tests for memory growth, file handles, database cursors and connections, network sessions, subprocesses and threads, concurrency, caches and queues, and temporary resources. This is a requirement for future implementations, not a claim that unimplemented background processing is already leak-free.

## Complete: Step 14B — Real Gmail durable classification workflow

**COMPLETE — `2556280 Add durable Gmail classification intake` (669 tests passed).** `dam scan --gmail --record-locally` explicitly enables private durable intake: authenticated Gmail profile → verified `SRC-...` → stable source-neutral `ITEM-...` → linked email observation, classification, and non-executable proposal. The same verified source and opaque Gmail message ID reuse one ITEM. Every admitted message is recorded, while Classification Queue work is created only for unresolved category teaching, not generic `Review=true`. Repeated intake is idempotent and deferred work remains durable; ordinary Gmail inspection remains non-durable.

SQLite v4 links new email observations to ITEMs without fabricating links for historical rows. Validated From and Subject may be retained in private SQLite; bodies, snippets, attachments, raw Gmail responses, OAuth material, and copied mailbox addresses are excluded. Local intake is atomic per message without a transaction across Gmail requests, and partial runs report committed work. Messages that leave Inbox between list and get are not admitted. The adversarial review fixed and regression-tested a direct-SQL integrity gap that could link an observation to the wrong source/native ITEM. The Gmail scope remains exactly `gmail.readonly`, the hard real-Gmail ceiling remains 10, and no Gmail write, action authority, or execution was added.

## Complete: Step 14C — Interactive teaching and configuration segmentation

**COMPLETE — `9a9d08b Add interactive classification teaching` (682 tests passed).** Post-intake teaching starts from durable Classification Queue work and a pinned, timestamped email observation without rereading Gmail or claiming current mailbox freshness. The CLI provides `dam teach list`, `show CWQ-...`, `preview CWQ-... --category KEY`, `save CWQ-... --category KEY --confirm-fingerprint ...`, `status TEACH-...`, and `resume TEACH-...`. Fingerprint-confirmed teaching saves an exact-sender classification rule; future source-state-dependent actions may require a fresh read. Classification teaching grants no mailbox-action authority.

One bounded Gmail run uses one effective rule/configuration state; rules do not switch during an in-flight read. After intake, teaching may establish a new effective state for local reevaluation and subsequent Gmail runs under existing learned-rule behavior. “Same session” means the same user interaction, not one mutable network scan. Earlier observations and evaluations retain their actual provenance. Rules and their changes are versioned, not destructive: evaluations should reference durable rule/version, CAT, configuration, and observation identities where available, copying only what historical truth or recovery requires.

**Classification evaluations are immutable historical records.** SQLite v5 adds stable `EVAL-...` identities and linked predecessor history. Reevaluation appends a new evaluation referencing its exact ITEM, persisted observation, result and CAT identity, rule/configuration provenance, confidence, human classification acceptance, structured Review reasons, time, and cause. The chain supports historical traversal and current-evaluation lookup without replacing history. Pre-v5 observations remain historical without fabricated evaluation IDs. Changing a rule must never rewrite a prior evaluation or make its historical meaning depend only on the rule's current form.

The general exact-ITEM reevaluation operation can be reused beyond teaching, including future reevaluation under newer rule versions; no bulk rule-version UI or command was added. It uses each ITEM's own suitable persisted evidence and reports insufficient evidence without fabricating it or treating unknown as absent. A future explicit source refresh may supply missing evidence. Teaching checks active Classification Queue work; selection optimizations cannot replace per-ITEM classification. Only an active permanent category with teaching satisfied resolves work; independent Review remains, and uncovered or conflicting items stay unresolved. Deferred work remains pending and its history survives later teaching.

Learned-rule YAML and SQLite cannot share one ACID transaction. Durable `TEACH-...` operations record confirmed intent, rule persistence, validated effective state, reevaluation progress, and completion. Saving a rule alone does not resolve queue work or prove reevaluation completed. Recovery recognizes already saved learning and resumes unfinished evaluations without duplicating completed history; an unloadable configuration leaves reevaluation pending. Learned-rule read/check/write is serialized and revalidated against stale state. Adversarial review added database enforcement of exact ITEM/observation teaching identity, discovery of unfinished operations, and recovery when a matching exact rule was already saved.

Step 14C does not pause an in-flight Gmail run, reread Gmail for teaching, group related items automatically, refresh missing source evidence, add bulk reevaluation commands, implement Rules Queue or action plans, grant authority, write to Gmail, execute Trash or unsubscribe, add a scheduler/provider/file classifier/GUI, or raise the real-Gmail ceiling above 10. Source-neutral ITEM/CWQ identity, minimized metadata, untrusted-content boundaries, user control, and future GUI compatibility remain required.

**Live acceptance passed.** Three real Gmail messages were durably admitted as ITEMs and initially needed teaching. One accepted exact-sender rule independently resolved two; the unrelated third remained unresolved until a second accepted exact-sender rule resolved it. The Classification Queue ended empty. Immutable predecessor/current EVAL chains, database integrity, recovery, and idempotency were checked. Human-accepted sender evidence stayed at confidence 0.90 and retained independent Review where required. No Gmail action occurred, and teaching granted no action authority or execution.

## Classification and handling work

### Classification Queue — “What is this?”

**PARTIAL.** Durable work identity, exact item membership, representative items, deferment, reevaluation, and immutable transition history exist. Opt-in verified Gmail intake now creates durable work for unresolved category teaching. A deferred item is pending human work, not forgotten. Uninspected items and generic Review alone do not create teaching work.

**COMPLETE:** Step 14C added post-intake teaching and local exact-member reevaluation. A work item may represent related items, but grouping is only a usability aid. There is no automatic grouping heuristic today; shared sender, provider, folder, or path does not prove shared classification. Every DAM Item remains individually identifiable and auditable. Teaching a representative item advances only members covered when each is reevaluated.

**PLANNED observability:** a reusable read-only application/domain audit-detail query should expose per-ITEM teaching outcomes, CWQ and current/predecessor EVAL references, classification, rule/version/configuration provenance, confidence, Review reasons, and why work resolved or remains unresolved. CLI and future GUI should consume the same query; exact presentation and command syntax remain open. This is an acceptance-discovered usability gap, not a Step 14C correctness failure.

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

**PLANNED temporary evidence:** during an active human classification interaction, a user may explicitly request minimum additional read-only evidence when persisted metadata is insufficient. Possible types include body text, recipients, selected headers, and attachment metadata without attachment contents. Such values are temporary, interaction-scoped, and discarded afterward; they must not enter SQLite, YAML, learned rules, logs, audit text, caches, or later function state. DAM may retain only the types of evidence consulted for provenance. No private-content hash is required by default, and later reevaluation must not claim discarded evidence is available. This capability is not implemented.

**PLANNED temporary presentation clearing:** when an interaction displaying temporary sensitive evidence ends, remove the evidence from DAM-controlled presentation surfaces where supported. Terminals must use the strongest supported screen/scrollback-clearing mechanism rather than assume `clear` or `cls` erases scrollback; GUIs must discard the temporary view and retained state. DAM can guarantee release of its own references, no prohibited DAM persistence/replay/history, and supported removal from surfaces it controls. It cannot claim secure erasure of external terminal scrollback, redirected output, terminal logging, screen recordings, or OS/process memory copies outside its practical control. This hard future requirement is also recorded in `AGENTS.md`; no evidence retrieval or clearing mechanism is implemented.

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
