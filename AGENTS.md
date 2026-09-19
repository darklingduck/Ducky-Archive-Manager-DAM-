# Ducky Archive Manager (DAM) — Codex Project Instructions

## Project Goal

Build **Ducky Archive Manager (DAM)** as a long-term personal information organization system.

The first implementation target is **email cleanup and organization**, especially Gmail, but the architecture should be designed so DAM can later expand to organize files, computers, documents, cloud storage, and other personal information sources.

DAM is not primarily a deletion tool.

Its purpose is to:

1. Discover what information exists.
2. Classify it.
3. Organize it.
4. Archive information that should be retained but does not need immediate attention.
5. Delete information only when an explicit rule and approval allow deletion.

Core philosophy:

> **DAM never destroys information without a rule and approval. It organizes first, archives second, and deletes last.**

Additional rule:

> **Previously established persistent rules remain active unless the user requests a change, subject to approval validity and safety checks. One-time approvals remain limited to their approved execution plans.**

---

# DAM Safety Rules

These rules are mandatory.

## Rule 1 — No permanent or unapproved deletion

DAM must never use permanent deletion. DAM must not move information to Trash unless a previously approved destructive rule explicitly allows it.

When DAM encounters a new sender, category, file type, or situation without an existing rule:

* classify it,
* report it,
* ask for a rule,
* do not delete it.

## Rule 2 — Organize before deleting

Normal processing order:

**Discover → Classify → Organize → Archive → Delete**

Deletion is always the last stage.

## Rule 3 — Preserve important records

Favor retention for:

* financial statements
* bills
* receipts
* tax records
* insurance
* medical information
* account/security notices
* family records
* legal/government information
* project files
* shipping confirmations
* purchases
* important correspondence

When uncertain, retain and classify.

## Rule 4 — Explicit user intent overrides defaults

The user's explicit instruction overrides DAM's behavioral defaults after DAM translates the instruction into a safe, reviewable technical rule. It does not bypass mandatory safety, approval, preview, or audit requirements, and cannot authorize permanent Gmail deletion.

The user should not need to know sender addresses, domains, Gmail query syntax, rule syntax, or other implementation details. DAM is responsible for discovering the relevant technical identifiers, distinguishing message types, proposing appropriate exceptions, and showing the effect of the proposed rule in dry-run mode.

If an instruction would create or substantially change a persistent destructive rule, DAM must show the proposed scope, counts, and important exceptions and obtain user approval before execution. Once approved, store the technical rule so the same decision does not require repeated approval unless its effective scope materially changes.

Example:

If the user says:

> Delete all Example Social Service messages.

DAM may propose moving Example Social Service messages to Trash even if its default policy would have archived them. Before approval, it should discover legitimate Example Social Service senders and separate promotional and social notifications from transactional, account, and security messages so the user can review the actual effect and any important exceptions.

## Rule 5 — Rules persist

Once the user approves a persistent classification or behavior, keep using it in future processing unless the user requests a change, subject to Rule approval and history and current-preview requirements. One-time approvals do not authorize future processing.

---

# DAM Processing Model

DAM should process data in phases.

## Phase A — Discovery

Determine:

* senders
* domains
* subjects
* frequency
* age of messages
* account relationships
* likely category
* promotional versus transactional content
* whether the sender is current, historical, unknown, trusted, or suspicious

Discovery is read-only: it must not modify Gmail or submit externally effectful unsubscribe requests.

Unsubscribe discovery should record mechanism evidence, subscription identities, protected-message exceptions, and count provenance. Distinguish label totals, observed unique messages, estimates, and complete inventories; record pagination limits and unresolved count discrepancies. Login requirements and category scope inferred from message evidence must be labeled as inferences, not verified facts.

## Phase B — Organization

Apply the approved hierarchy and archive messages that no longer need to remain in the Inbox.

Messages should remain searchable.

## Phase C — Cleanup

Use approved cleanup rules for:

* obvious junk
* expired promotions
* duplicate notifications
* unwanted newsletters
* old job alerts
* explicitly approved sender deletions

Historical cleanup after unsubscribe is a separate message-action plan governed by Unsubscribe Management, exact-ID approval, and the shared safety infrastructure. Successful unsubscribe never authorizes sender-wide Trash.

## Phase D — Verification

After processing, search again for:

* sender variants
* alternate domains
* leftover inbox messages
* misclassified mail
* newly discovered senders

Report unresolved items instead of guessing. Verification may discover additional messages or subscription streams, but must not expand an approved execution set. Record applied actions separately from verified results and unresolved verification failures.

---

# Natural Language Intent Layer

DAM should eventually provide a layer between the user and the rule engine:

**User Request → Intent Interpretation → Discovery → Proposed Technical Rule → Safety Evaluation → Dry Run → User Approval when required → Approved Rule / Execution Plan → Execution → Audit / Verification**

The user expresses intent in normal language. DAM translates that intent into safe technical rules.

For a request such as:

> Delete all Example Social Service messages.

DAM should:

1. Discover sender addresses and domains associated with Example Social Service messages in the mailbox.
2. Determine which appear to be legitimate Example Social Service sources without automatically trusting unfamiliar senders or following links.
3. Identify promotional, social-notification, transactional, account, and security message types.
4. Generate a proposed technical rule with important exceptions where appropriate.
5. Evaluate the proposal against DAM's safety policies and existing rules.
6. Run the proposal in dry-run mode.
7. Show affected counts, representative metadata, conflicts, and important exceptions.
8. Obtain appropriate approval for the effectful plan; creating or substantially changing a persistent destructive rule requires renewed approval of its scope.
9. Store the versioned rule and approval history, explicitly distinguishing persistent authority from a one-time execution plan.

Natural-language interpretation must never bypass discovery, dry-run, confidence requirements, approval requirements, or auditing. Ambiguous intent should produce a conservative proposal or a request for clarification rather than a destructive assumption.

---

# Classification Hierarchy

DAM uses hierarchical categories rather than unrelated top-level labels.
The following examples are entirely synthetic, not a user's account inventory:

* Home
  * Utilities / Example Energy Provider
  * Internet / Example Internet Provider
  * Family / Example Family Member / Support Services
  * Pets / Example Veterinary Practice
* Finance
  * Bank / Example Bank
  * Mortgage / Example Mortgage Provider
  * Insurance / Example Insurer
  * Investments / Example Brokerage
* IT & Development
  * Hosting / Example Hosting Provider
  * Development / Example Code Service
  * Employment / Inactive
* Entertainment
  * Gaming / Example Game Service
  * Streaming / Example Streaming Service
* Health & Benefits
  * Pharmacy
  * Billing
  * Appointments
* Accounts & Security
  * Example Account Provider
* Research
  * Example Research Publisher
* Promotions
  * Home
  * IT
  * Unsubscribe Candidates / Employment

---

# Configurable User Rules

These are synthetic policy examples, not established user decisions or approvals.
Keep actual personal preferences and provider inventories in private configuration.

## Employment

If a user explicitly configures job-search mode as inactive, unsolicited recruiter
mail, job alerts, staffing solicitations, and resume requests may be classified as
**IT & Development / Employment / Inactive**. Historical messages may be archived
under an applicable approved rule. Recurring marketing may also be tagged
**Promotions / Unsubscribe Candidates / Employment**.

Job-search state must be configurable and may change; never infer employment status.

## Promotions

Retain receipts and shipping notices. Promotions generally do not need Inbox
priority. For a synthetic Example Shop stream, an approved retention policy may
retain the newest useful promotion and identify older promotions as candidates.
Shipping notices remain retained indefinitely until an explicit retention period
is configured. Retention expiration alone never authorizes Trash.

## Historical accounts

Historical services retain security notices, ownership records, useful transactions,
and important correspondence. Their marketing generally lacks Inbox priority.
Example Legacy Service is a fictional historical account. Historical never
implicitly means delete.

## Trusted/current services

Current status and trust must come from private user-editable configuration and
supporting evidence, not public hardcoded provider lists. Example Utility Service
is a fictional current provider; its status does not authorize destructive actions.

## Explicit unwanted sources

A user may identify a fictional Example Social Service or Example Newsletter as
unwanted. Represent these decisions in versioned user-editable rules with the
required discovery, protection exceptions, previews, and approval history.

---

# DAM Rule Engine

Rules should be data-driven.

Prefer something like:

```yaml
rules:
  - id: synthetic_social_cleanup
    match:
      sender_domain:
        - social.example.invalid
    action: trash
    approved: true

  - id: job_alerts_inactive
    match:
      keywords:
        - recruiter
        - job alert
        - career opportunity
        - send your resume
    classify:
      - "IT & Development/Employment/Inactive"
      - "Promotions/Unsubscribe Candidates/Employment"
    action: archive
    approved: true
```

These abbreviated YAML examples illustrate syntax, not actual approval or executable destructive policies. A sender-domain match alone does not establish subscription identity or override protected-message checks. Live actions require the approval records, exceptions, confidence, and current previews specified below.

Rules should support:

* sender email
* sender domain
* subject keywords
* body keywords
* Gmail category
* message age
* labels
* current/historical status
* allow-list
* deny-list
* action
* retention period
* user approval state
* approval metadata and source
* notes
* rule priority
* rule version
* effective scope fingerprint
* stable subscription identity and stream-matching evidence
* protected-message exceptions
* relevant unsubscribe and safety-policy versions

Do not bury behavioral rules inside Python conditionals unless necessary.

## Rule precedence and conflicts

When multiple rules match, apply this precedence:

1. Safety restrictions and Critical, Priority, or Review overrides
2. Explicit retention and allow-list rules
3. Approved, sender-specific or service-specific user rules
4. More specific classification and action rules
5. General category and fallback rules

Within the same level, use explicit rule priority and then specificity. If equally authoritative rules conflict, choose the least-destructive result and mark the message for Review. A general cleanup rule must not override a more specific retention, security, or actionable-message rule.

## Rule approval and history

Approval records should include:

* approval status
* approving user or source
* approval timestamp
* rule version
* scope fingerprint or equivalent description of the approved effective scope

Approval must distinguish a one-time execution plan from a persistent rule. A one-time approval does not authorize future messages or subscription streams. Bind effectful approvals to the mailbox/account identity, concrete scope, preview fingerprint, and relevant rule and policy versions. Combined unsubscribe and historical-cleanup approval is allowed only under the conditions in Unsubscribe Management.

Rules must be versioned. Preserve prior versions and approval history instead of silently overwriting established rules. Disablement, replacement, and approval changes must be auditable.

A material change to a rule's effective scope invalidates its destructive-action approval. Examples include adding senders or domains, broadening message types or date ranges, weakening exceptions, or changing the action to a more destructive one. Non-material changes such as notes or formatting do not require renewed approval.

---

# Actions

DAM should support at least these actions:

* no_action
* classify
* label
* archive
* mark_priority
* mark_review
* mark_unsubscribe_candidate
* trash

The actions have distinct meanings:

* **archive** removes the Inbox label while keeping the message in the mailbox and searchable.
* **trash** moves a message to Gmail Trash. This is destructive because recovery is time-limited and Gmail may permanently remove it according to Gmail's Trash retention behavior.
* **permanent deletion** irreversibly deletes a message through the Gmail API and is prohibited, not a supported DAM action.

DAM must not use Gmail's permanent-delete API. Trash is the only supported deletion action and requires a high-confidence match to an approved destructive rule. Audit output and user-facing language must not describe Trash as permanently recoverable.

Unsubscribe is a separate externally effectful action, even when it does not modify Gmail. The unsubscribe subsystem must not directly Trash, archive, or label messages; historical cleanup uses shared message-action, approval, audit, and verification infrastructure.

DAM should operate on individual Gmail messages by default. Thread-wide actions require separate, explicit design and approval because a thread may contain messages with different classifications.

---

# Priority System

DAM should identify actionable mail separately from ordinary records.

Suggested states:

* Critical
* Priority
* Review
* Routine
* Archived

Examples of Priority:

* failed payments
* suspicious sign-ins
* account-lock warnings
* overdue bills
* unresolved family-service issues
* legal deadlines
* account cancellation problems

Important messages should not be archived automatically if action is still required.

Critical, Priority, and Review are safety overrides. Messages in these states must not be archived or moved to Trash automatically unless a more specific approved rule explicitly covers the actionable condition. Historical cleanup under Unsubscribe Management always preserves these messages; a general priority exception cannot authorize their inclusion in that cleanup plan.

---

# Unknown Sender Handling

For an unknown sender:

1. Do not delete.
2. Inspect sender/domain.
3. Examine subject and message content.
4. Determine likely category.
5. Assign:

   * Current
   * Historical
   * Promotional
   * Unknown
   * Suspicious
6. Place in Review if confidence is insufficient.

Example:

An email from an unfamiliar foreign healthcare service should not automatically be trusted or clicked.

Unknown does not equal spam.

---

# Confidence

Classification and action selection should have separate confidence values.

Example:

```json
{
  "category": "Finance/Mortgage/Example Mortgage Provider",
  "classification_confidence": 0.98,
  "action_confidence": 0.96,
  "reason": "Sender domain and recurring mortgage statement pattern"
}
```

Suggested thresholds:

* >= 0.95: auto-classify if an existing rule permits
* 0.75–0.94: classify but flag for review
* < 0.75: do not act automatically

These thresholds apply independently to classification and action confidence. A message may have a high-confidence category but a low-confidence action.

Confidence values must retain supporting evidence and limitations; a numeric score alone is not proof of safety. Unsubscribe planning should distinguish mechanism-validation confidence, subscription-identity confidence, message-classification confidence, and action confidence. Positive marketing identification and protected-type checks are required; sender identity, an unsubscribe header, a Gmail category, or absence of a keyword alone is insufficient. Inspect minimum additional content or preserve for Review when evidence is inadequate.

Destructive actions should require all of the following:

* high classification confidence
* high action confidence
* approved rule
* no applicable safety override or higher-precedence conflict

---

# Audit Log

Every DAM action should be logged.

Store:

* timestamp
* message ID
* sender
* subject
* matched rule
* classification
* previous state
* action performed
* reason
* classification confidence
* action confidence
* user approval source
* rule version
* run ID
* mode: dry_run or live
* message-action outcome: proposed, attempted, applied, already_applied, skipped, failed, verified, verification_failed, indeterminate, or reversed
* whether the mailbox was modified (`mailbox_modified`)
* external subscription-change status (`subscription_changed`: true, false, or unknown), separate from mailbox modification
* subscription identity, plan/preview identity, policy versions, and verification evidence where applicable

Example:

```json
{
  "timestamp": "2026-09-02T12:30:00",
  "message_id": "abc123",
  "rule": "synthetic_social_cleanup",
  "action": "trash",
  "reason": "User explicitly approved deletion of Example Social Service mail"
}
```

The log should make it possible to answer:

* What did DAM do?
* Why?
* Which rule caused it?
* Can we reverse it?

Dry-run proposals are not completed actions. Audit-preview records must clearly state that the outcome is proposed and that neither the mailbox nor external subscriptions were changed. Statistics must likewise distinguish proposed actions from applied actions.

Unsubscribe audit records must additionally identify the representative message, mechanism and scope evidence, approval source, destination domain, protected endpoint reference/fingerprint, transport attempt, HTTP/API result, scope-specific response interpretation, unexpected behavior, and lifecycle state. Record later delivery observations separately. Never infer `subscription_changed=true` solely from submission; preserve unknown outcomes.

Use durable checkpoints before effectful requests, immutable event history, and restart reconciliation. SQLite transactions should maintain consistent local audit and run state; external requests cannot be made atomic with local storage. Derive reports and counts from reconciled history rather than overwriting prior evidence.

Processing must be idempotent. Repeating a scan or safely retrying an execution must not create duplicate labels, duplicate action records, or inflated statistics. Use run IDs and stable message/action identities to distinguish retries, already-applied actions, and genuinely new work.

---

# Dry Run Mode

This is mandatory.

DAM should support:

```bash
dam scan --dry-run
```

Dry-run should show:

* messages matched
* proposed category
* proposed action
* matched rule
* classification confidence
* action confidence
* counts

It must make no mailbox changes or external subscription changes. An unsubscribe dry run must not visit unsubscribe destinations or submit requests.

Normal development should default to dry-run until a feature has been tested.

A live execution based on a reviewed dry run must be tied to the exact rule version and message IDs that were previewed. If the preview is stale or its effective scope has changed, DAM must require a new dry run.

Effectful previews must be immutable and include:

* mailbox/account identity, preview ID, creation time, and explicit expiration time in UTC
* exact individual message IDs for message actions and concrete subscription identities for unsubscribe
* mechanism and destination evidence, preservation exceptions, and any cleanup prerequisites
* relevant rule, classifier, identity-resolution, and safety/transport-policy versions
* a semantic scope fingerprint covering actions, identities, IDs, conditions, and exceptions; notes or formatting alone must not change effective scope
* relevant message-state snapshots: sender, subject, receipt timestamp, subscription evidence, protection/classification state, labels, and content evidence or protected hashes where needed

Define preview validity durations in configuration before live execution; no implicit or indefinite freshness is allowed. Expiration, account mismatch, material policy/version changes, broader scope, or weakened exceptions invalidate execution and require a new preview. Revalidate each target immediately before acting. Newly protected messages, changed subscription evidence, or materially changed content/state must be preserved and reported individually. A skipped ID never permits substitution with another ID. Only explicitly documented non-material state changes may be tolerated; uncertainty blocks the individual action. Already-applied actions require state reconciliation, not duplicate writes.

---

# Retention

Retention periods must use explicit durations or dates rather than phrases such as "several years." A retention rule should define:

* the event from which age is measured, such as message receipt time or last relevant activity
* the duration or expiration date
* timezone handling
* protected message types and exceptions
* the action proposed when retention expires

Expiration of a retention period does not by itself authorize Trash. A separate approved destructive rule is still required.

---

# Statistics

Maintain useful statistics.

Examples:

* Inbox count before
* Inbox count after
* Messages classified
* Messages archived
* Messages trashed
* Messages requiring review
* Messages per category
* messages per sender
* oldest inbox message
* recurring sender volume

The user specifically wants DAM to retain cleanup statistics.

Retain unsubscribe lifecycle counts and later delivery observations separately from message-action counts. Distinguish proposed, attempted, applied, verified, skipped, failed, and indeterminate outcomes without inflating retries. Record subscription-stream counts, inventory completeness, and preserved-message counts with verification coverage. Trash count is not Inbox reduction; report actual Inbox-label removals separately.

---

# Architecture

Keep the project modular.

## Orchestration and service boundaries

All DAM execution must eventually pass through one Orchestrator/controller. The normal entry point starts a continuous orchestrated session; explicit commands such as `dam scan` and `dam teach list` remain available in direct/single-operation mode, but use the same Orchestrator and stop after the requested operation. The same service contract and business behavior apply in either mode. Do not add a separate direct-command execution path or require each function to inspect CLI arguments to decide whether the workflow continues. The Orchestrator owns session lifecycle, workflow/path state, current and completed stages as appropriate, permitted transitions, interruption, continuation or stop, and future resume. It invokes only implemented and permitted stages. **Services/functions know how to perform DAM operations; the Orchestrator knows when and why they run.** The Orchestrator does not own Scan, Classification, Teaching, Rules, or Action business logic.

Major workflow functions must be mutually blind. Scan must not invoke Classification as a sibling stage; Classification must not invoke Rules; Teaching must not invoke another sibling to advance the workflow. A function receives defined inputs, performs its own responsibility, and returns a typed result to the Orchestrator. It must not call or notify the Orchestrator when finished, choose its next sibling, or need knowledge of adjacent stages or orchestration mode. Reusable lower-level components are permitted, but must not become covert workflow or data channels between functions.

The Orchestrator owns or receives a stable Session ID for correlation. **Session ID identifies context; it does not confer authority.** Possessing it must not grant broad access to ITEMs, source content, classifications, rules, or function-private work. Keep session-control state (stage, route, interruption, resume position, and limited routing result) separate from domain/work data. The Orchestrator has only the access needed to start, route, interrupt, stop, and identify/resume a session. For resume, it invokes the appropriate function, which loads its own outstanding work through its constrained interface. Detailed session persistence and schema remain for later design.

Prefer stateless shared components. Any necessary state must have explicit ownership, lifetime, readers, writers, and cleanup. Avoid module globals, convenience singletons, implicit caches, retained previous ITEMs or source content, and reusable presentation state. Adversarial tests should use distinctive sentinels across functions, ITEMs, interactions, and sessions to detect residual data in objects, caches, globals, logs, exceptions, temporary files, or presentation state.

Functions must not directly own terminal or GUI rendering. They return typed results or presentation events containing only data intentionally permitted for display. A Writer/Presenter boundary renders only that supplied data; a Session ID may correlate output but must not let the Writer query arbitrary session, domain, or source data or introspect function internals. Presentation contracts need explicit sensitivity and lifetime semantics. Temporary/private source content must not persist through logs, Writer history, caches, exceptions, audit text, or later function state. User input should likewise be abstracted from terminal `input()` so CLI, future GUI, and tests can use the same domain operations. Exact APIs and class names remain undecided.

Continuous operation should finish a bounded scan/intake under one effective configuration, then offer applicable unresolved Classification Queue work without requiring a sequence of separate commands. The user may Teach or Defer, request permitted evidence, continue when allowed, interrupt, stop safely, or later resume. Only the Orchestrator decides the next valid transition from typed stage outcomes. Teaching remains post-intake; local reevaluation may use new knowledge, while a later Gmail scan is a new bounded run. Direct commands remain available for explicit control, scripting, testing, and troubleshooting.

## Temporary classification evidence

DAM remains metadata-first; normal scans must not automatically retrieve every body. When persisted evidence is insufficient for an active human classification interaction, the user may explicitly request minimum additional read-only evidence, potentially body text, To/Cc, Reply-To or selected headers, or attachment names/types/metadata without attachment contents. Exact evidence types remain open. Expanded source content is temporary, sensitive, scoped to that interaction, and released when it ends. It must not be persisted in SQLite, YAML, learned rules, logs, audit text, caches, or other durable storage, or become accessible to later functions through a Session ID or shared component. DAM may record which *types* of temporary evidence informed a human decision without retaining the values. Do not create content hashes/fingerprints by default. Later reevaluation must report discarded evidence as unavailable rather than pretend it remains stored. Retrieval, scope, and lifetime require their own reviewed implementation; documenting this does not enable new source reads.

Suggested structure:

```text
dam/
├── README.md
├── AGENTS.md
├── pyproject.toml
├── config/
│   ├── categories.yaml
│   ├── rules.yaml
│   └── settings.yaml
├── src/
│   └── dam/
│       ├── __init__.py
│       ├── cli.py
│       ├── models.py
│       ├── rules.py
│       ├── classifier.py
│       ├── gmail.py
│       ├── actions.py
│       ├── audit.py
│       ├── stats.py
│       └── unsubscribe/
│           ├── __init__.py
│           ├── models.py
│           ├── discovery.py
│           ├── identity.py
│           ├── policy.py
│           ├── planning.py
│           ├── transport.py
│           └── executor.py
├── tests/
│   ├── test_rules.py
│   ├── test_classifier.py
│   └── fixtures/
└── data/
    └── .gitkeep
```

Do not store Gmail credentials, API tokens, OAuth secrets, message bodies, or other private data in Git.

`dam.unsubscribe` is a dedicated future subsystem. Its models represent identities, evidence, mechanisms, and lifecycle states; discovery extracts evidence without visiting links; identity resolves streams conservatively; policy enforces protection and interaction restrictions; planning produces immutable approval-bound plans; transport performs bounded validated requests; executor coordinates approved execution, checkpoints, and result interpretation.

Reuse shared approval, audit, statistics, message-action, and verification infrastructure. Gmail supplies message evidence through its adapter; unsubscribe execution must not directly perform Gmail cleanup. Keep durable subscription identities, evidence references, plans, approvals, attempts, observations, and outcomes in local SQLite. The architecture anticipates other information sources without coupling the shared rule engine to Gmail.

---

# Initial Technology

Use Python unless there is a strong technical reason otherwise.

Prefer:

* Python 3.12+
* type hints
* dataclasses or Pydantic models
* pytest
* YAML configuration
* SQLite for local metadata/audit history
* Google Gmail API with OAuth

Avoid unnecessary framework complexity.

This should initially work well as a CLI application before building a GUI.

---

# Privacy

DAM processes highly personal data.

Requirements:

* credentials never committed
* `.env` ignored
* OAuth tokens stored outside repository or in a protected local directory
* logs should minimize message-body storage
* avoid storing full email bodies unless necessary
* begin with headers and metadata; retrieve snippets or bodies only when needed for classification
* fetch the minimum body content required and prefer plain text
* never automatically follow message links, download unrelated remote content, or load tracking resources; only approved unsubscribe execution may contact its validated destination under Unsubscribe Management
* do not retrieve attachments by default
* redact credentials and sensitive tokens
* make local-first processing the default
* do not render unsubscribe pages or load their images, scripts, attachments, or unrelated resources
* keep sensitive unsubscribe URLs and recipient tokens out of normal reports, logs, and Git; use redacted destination summaries and protected references
* store necessary endpoints only in access-restricted protected storage, not as subscription identities; discard unnecessary temporary payloads
* before retaining endpoints in a production subsystem, define an explicit retention duration, deletion event, access controls, and audit-evidence retention policy; no indefinite retention by default

If a connector requires returning a full MIME text payload for inspection, process it transiently and retain only minimum necessary evidence. Record that technical limitation; do not treat it as permission to fetch attachments or remote content.

OAuth permissions should follow least privilege:

* Milestone 1 must use Gmail read-only permissions.
* Request modification permissions only when a later approved milestone enables write operations.
* Never request or use permanent-delete capability.
* HTTPS unsubscribe execution does not itself justify expanding Gmail OAuth permissions. Mailto or reply-based unsubscribe introduces sending authority and requires a separately approved capability and least-privilege permission review.

---

# Write Safety and Rollback

Before enabling any Gmail write operation, DAM must support rollback planning and conservative operational limits. External unsubscribe execution also requires reviewed plans, bounded requests, durable audits, conservative limits, and recovery planning, even when Gmail access remains read-only.

Rollback support should record the previous labels and Inbox state, maintain a run-level manifest of changes, support reversal of DAM-applied label and archive operations, and verify the result. Trash recovery is time-limited and must be represented separately from reliably reversible operations.

Early write-capable versions should:

* limit messages per run
* require a reviewed, current dry-run preview
* bind execution to the previewed message IDs and rule versions
* stop or pause after configurable error thresholds
* verify applied changes
* report partial success and failures without silently retrying destructive actions

For historical cleanup, maintain a preservation manifest of excluded message IDs, reasons, relevant prior state, and verification coverage. Assert that approved and preserved ID sets are disjoint. Distinguish “excluded from DAM writes” from “verified unchanged”; perform targeted before/after preservation checks where collateral effects are possible, and report the limits of verification.

Verify Trash membership per target after execution and reconcile final run results. An accepted API response is not verified mailbox state. Record applied, skipped, failed, indeterminate, and verification-failed outcomes separately; do not claim complete success while unresolved outcomes remain.

Never blindly retry an effectful unsubscribe request after timeout or uncertain transmission. Distinguish proven pre-submission transport failure from an indeterminate outcome. Reads may use bounded backoff for transient errors; effectful retries require outcome reconciliation, unchanged approval/scope, and a mechanism-specific safe-retry policy. Reconcile current Gmail state before any Trash retry; preserve stable action identities across recovery. Stop or pause at configured error thresholds, and do not silently retry destructive actions.

Unsubscribe has no guaranteed rollback; resubscription may be a separate effectful action requiring approval. Trash recovery remains time-limited and must never use permanent deletion.

---

# Development Method

Do not attempt to build the entire project at once.

Work incrementally.

For every significant change:

1. Explain what you intend to change.
2. Identify the files involved.
3. Make one logical change.
4. Run tests.
5. Show the results.
6. Commit only after the feature works.

Avoid giant rewrites.

---

# First Development Milestone

Build a safe read-only prototype.

The first version should:

1. Authenticate to Gmail.
2. Read Inbox metadata.
3. Retrieve:

   * message ID
   * sender
   * subject
   * date
   * labels
4. Load DAM rules from YAML.
5. Match messages against rules.
6. Produce a proposed classification.
7. Produce a proposed action.
8. Print statistics.
9. Write an audit-preview log.
10. Make **no Gmail modifications**.

Milestone 1 must use Gmail read-only OAuth permissions, operate on individual messages, and begin with message headers and metadata. Body or snippet retrieval may be added only when needed for classification and must follow the privacy requirements above.

Anticipate read-only unsubscribe discovery through header extraction, subscription-evidence models, protection exceptions, count provenance, and audit previews. A read-only discovery extension may inspect minimum required content, but must not visit unsubscribe destinations, send unsubscribe mail, or execute any external opt-out. Unsubscribe execution is not part of Milestone 1.

Expected command:

```bash
dam scan --limit 100 --dry-run
```

Synthetic example output:

```text
Scanned: 100
Classified automatically: 73
Needs review: 18
Priority: 4
Unmatched: 5

Proposed actions:
Archive: 61
Keep Inbox: 12
Trash: 9
No Action: 18
```

No Gmail write operations should be enabled until the dry-run classifier has tests and its output has been reviewed.

---

# Second Milestone

After the dry-run system is reliable:

Implement controlled Gmail actions for:

* labeling
* archiving
* priority marking

Do **not** enable trash automatically at the same time.

Trash should be introduced separately and only for approved rules.

Before enabling these write operations, implement idempotency, run manifests, conservative execution limits, rollback for labeling and archiving, and post-action verification.

---

# Third Milestone

Add:

* duplicate detection
* retention rules
* expanded read-only unsubscribe candidate discovery and subscription identity resolution
* sender statistics
* historical-account state
* better confidence scoring
* rule-editing commands
* natural-language intent interpretation and technical-rule proposal workflow

Example:

```bash
dam rule list
dam rule show synthetic_social_cleanup
dam rule disable synthetic_social_cleanup
dam rule approve new_rule_23
```

---

# Unsubscribe Management

Unsubscribe management is a dedicated future DAM subsystem. Discovery, external opt-out, and historical message cleanup are distinct plans and capabilities. Approval of this design does not approve any subscription execution or Gmail operation.

## Subscription identity and discovery

Assign a stable local subscription ID tied to the mailbox/account, publisher, mailing service, audience/list/category, and supporting evidence. Sender address or domain alone is not a subscription identity. A sender may carry multiple independently identifiable streams, and one stream may use multiple senders. Provider migrations, shared platforms, or matching brands do not establish equivalent scope.

List-ID, provider audience/publisher fields, authentication, and body descriptions are evidence, not universally authoritative identifiers. Opaque provider fields may identify campaigns rather than subscription units; retain their uncertain semantics and confidence. Rotating recipient tokens and endpoint fingerprints must not define stream identity.

Inspect, in order:

1. RFC/List-Unsubscribe headers and available destinations.
2. List-Unsubscribe-Post / one-click declarations and authentication evidence.
3. Body unsubscribe links.
4. Preference-center/email-preferences links.
5. Sender website/account-setting evidence controlling marketing email.

Do not visit destinations during discovery. Parse multiple destinations conservatively and distinguish header mailto, body links, preference centers, account settings, and reply instructions. Never repair unresolved templates or guess malformed URLs. Report mechanisms as advertised, parsed, validated, or executed; record destination domains without exposing recipient tokens. Record missing evidence as unknown, not proof of absence or a single category.

## Initially supported execution

Initially automate only properly validated standards-based HTTPS one-click unsubscribe. Validate the List-Unsubscribe HTTPS destination, `List-Unsubscribe-Post: List-Unsubscribe=One-Click`, and the applicable standards requirements. Correlate authentication evidence with the same valid DKIM signature covering both unsubscribe headers; unrelated passing signatures or mere header presence are insufficient. Validate sender/publisher relationship and destination against the reviewed discovery scope. Preserve raw endpoint semantics without speculative rewriting.

Submit the prescribed HTTPS POST with `List-Unsubscribe=One-Click` and the appropriate form content type. Do not issue a preliminary GET, use account credentials/cookies, load remote resources, or interact with returned forms. Use TLS validation, bounded timeouts/response sizes, approved destinations, and safeguards against local/private-network targets. Suspicious or inadequately authenticated endpoints require Review.

Preference centers, web-only flows, broader redirect interaction, login-required flows, CAPTCHA, mailto, reply-based unsubscribe, and provider-specific mechanisms are later capabilities, separately gated by implementation review and concrete approval. Do not request sending permissions for one-click execution.

## Lifecycle and confirmation

Record these unsubscribe states explicitly:

* `attempted`: an approved execution attempt was initiated; record whether transmission occurred.
* `submitted`: the mechanism accepted the request without explicit scope-specific completion evidence.
* `confirmed`: explicit evidence confirms unsubscribe for the approved subscription scope.
* `failed`: a definite failure was observed; do not infer that every failure proves no external effect.
* `skipped`: execution or further interaction was withheld for a recorded reason; note any request already transmitted.
* `indeterminate`: transmission or resulting subscription state cannot be reliably established.

A successful HTTP/API submission must not be described as confirmed without explicit scope-specific evidence. Interpret responses according to the validated mechanism; HTTP 2xx, generic text, or a regex match alone does not prove confirmation. Unexpected forms, oversized responses, ambiguous scope, or login/security challenges must stop further interaction and remain unresolved where effects are uncertain.

Record later delivery observations separately, including observation time, stream evidence, and any known processing delay. Absence of observed mail is not proof of permanent suppression. Track `subscription_changed` separately from `mailbox_modified`; accepted submission may leave actual subscription change unknown.

## Failure, fallback, redirects, and manual flows

Failed, malformed, expired, ambiguous, or unavailable mechanisms must fail safely. Do not silently switch links, categories, methods, mailto, replies, or account settings. A fallback requires its own discovered evidence, destination/scope validation, supported capability, and approval coverage. HTTP 404 does not authorize trying related URLs or broadening scope. No automatic cleanup follows failed or indeterminate unsubscribe under a success-dependent plan.

Default to no automatic redirects. Record a redacted redirect destination without following it. Future supported redirects must validate every hop, destination, TLS scheme, request method, and scope; enforce bounded hop counts and approved destinations. Stop on HTTPS downgrade, unexpected host or scope, credentials, or broader interaction. Never forward tokens or credentials to unapproved destinations.

Login-required flows stop for manual review. Entering credentials requires separate explicit approval. Do not bypass CAPTCHA, anti-bot protections, or other access controls. If a later approved preference center asks why, use “Too much email” or the closest equivalent. Stop all marketing categories within the approved scope only when their meaning is clear; never disable transactional, security, billing, application, or account-required communication. Ambiguous “all communications” options must stop rather than be guessed.

## Historical cleanup and combined approval

Historical cleanup is a separate shared DAM message-action plan. `dam.unsubscribe` must not directly Trash messages. Successful unsubscribe does not authorize sender-wide Trash or processing other subscription streams.

Require positive matching to the approved subscription stream, protected-message checks, high evidence-backed classification/action confidence, and exact individual message IDs. Preserve Critical, Priority, Review, transactional, account-required, security, billing, application, government/benefit, and direct-correspondence messages. Retain mixed or uncertain messages even when they contain unsubscribe mechanisms.

One approval may authorize both unsubscribe and conditional historical cleanup only when:

* the subscription scope is concrete and the mechanism is previewed;
* exact historical message IDs and preservation exceptions are shown;
* relevant rule/policy versions and preview fingerprints are bound to approval;
* approval explicitly covers both external unsubscribe and Gmail Trash;
* the cleanup condition explicitly identifies whether `submitted` or `confirmed` is sufficient.

Otherwise, separate approval stages are required. A combined plan may preview historical IDs before unsubscribe, but execution must enforce the approved success condition and revalidate every ID. Messages discovered later require a new preview/approval unless separately authorized through a current approved plan. One-time approval does not create persistent sender-wide cleanup authority.

Use shared stale-preview checks, preservation manifests, durable action audits, retry/recovery controls, and post-action verification. Only approved Gmail Trash is permitted; permanent Gmail deletion remains prohibited.

## Capability sequence and acceptance case

Implement incrementally, independently of broad milestone feature lists:

1. Read-only unsubscribe discovery: evidence extraction, identities, inventory limitations, protection checks, and audit previews. This may extend Milestone 1 without enabling execution.
2. Controlled validated HTTPS one-click execution: gated on tested validation, approval binding, restricted transport, lifecycle interpretation, durable recovery, and audit support. This is outside Milestone 1.
3. Separately gated historical cleanup: shared exact-ID approval, current previews, preservation manifests, immediate checks, conservative limits, and verified Trash actions. Do not bundle Trash enablement with labeling/archive or unsubscribe enablement.
4. Later provider-specific and preference-center support: separately reviewed capabilities and approvals; manual treatment remains the default for unsupported interactions.

Use an entirely synthetic acceptance scenario, not actual mailbox statistics:

* Two opt-out requests are submitted without explicit confirmation evidence.
* A separate request returns HTTP 404 without fallback or related cleanup.
* Three exact synthetic historical promotional IDs are approved for Trash.
* Simulated Trash membership is verified for each approved ID.
* Protected synthetic messages remain excluded and no permanent deletion occurs.

The scenario tests separate streams, protection exceptions, exact-ID approval,
revalidation, and distinct attempted/applied/verified records. It does not establish
production capability or complete mailbox coverage. Future tests must also cover
malformed headers, uncertain identities, uncorrelated authentication, stale previews,
protected messages, redirects, login/CAPTCHA, indeterminate transmission,
verification failure, crash recovery, and safe idempotency.

---

# Long-Term Vision

DAM should eventually become a general information-management engine capable of handling:

* Gmail
* local files
* downloads
* documents
* photos
* cloud drives
* backups
* old computers
* duplicate data

The same philosophy should apply everywhere:

> Discover first.
> Classify second.
> Organize third.
> Archive fourth.
> Delete only under an approved rule.

Build the email system in a way that does not tightly couple the rule engine to Gmail.

A future source should be able to implement a common interface such as:

```python
class DataSource:
    def discover(self): ...
    def classify_metadata(self): ...
    def apply_action(self): ...
```

The Gmail connector would then be one implementation rather than the entire DAM architecture.

---

# Codex Working Instruction

Before writing code, inspect the repository and summarize:

1. What currently exists.
2. What can be reused.
3. What conflicts with this specification.
4. The smallest logical first implementation step.

Do not delete or replace existing work without explaining why.

For the first implementation, prioritize **safety, auditability, and dry-run classification over automation speed**.
