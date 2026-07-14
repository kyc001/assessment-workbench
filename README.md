# Assessment Workbench

> Verifier-centric multi-agent evaluation, structured reward candidates, and replayable trajectories for assessment generation.

[中文文档](README.zh-CN.md)

[![Python 3.12+](https://img.shields.io/badge/Python-3.12%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/GUI-React-20232A?logo=react&logoColor=61DAFB)](frontend/)
[![License](https://img.shields.io/badge/License-Apache--2.0-3DA639)](LICENSE)

Assessment Workbench investigates a practical systems question: **how can a multi-agent generation process expose reliable verification signals, structured feedback, and replayable trajectories instead of collapsing into one opaque prompt?**

Assessment generation is the concrete environment: a Writer proposes questions, an Independent Solver derives answers, a Rubric Builder defines scoring contracts, specialized Reviewers act as a verifier ensemble, and an Arbiter turns verifier disagreement into targeted revision actions. The runtime records every version, finding, action, failure, retry, and checkpoint as an auditable trajectory.

The current project is best described as **evaluation, feedback, and trajectory infrastructure for future RLVR and Agentic RL experiments**. It does not yet train a policy, optimize a reward model, or claim measured resistance to reward hacking.

## Verifier-Centric Research Framing

```mermaid
flowchart LR
    Env["Assessment-generation environment\ncourse evidence + exam constraints"] --> Policy["Candidate policy\nQuestion Writer"]
    Policy --> Candidate["QuestionVersion"]
    Candidate --> Solver["Independent Solver"]
    Candidate --> Rubric["Rubric Builder"]
    Candidate --> Reviewers["Verifier ensemble\nmathematical, solvability, pedagogy, subject, rubric"]
    Solver --> Evidence["SolutionVersion"]
    Rubric --> Contract["RubricVersion"]
    Evidence --> Reviewers
    Contract --> Reviewers
    Reviewers --> Disagreement["Structured findings + verifier disagreement"]
    Disagreement --> Arbiter["Arbiter / feedback router"]
    Arbiter -->|"pass"| Accepted["Accepted Bundle"]
    Arbiter -->|"retry_problem"| Policy
    Arbiter -->|"retry_solution"| Solver
    Arbiter -->|"retry_rubric"| Rubric
    Arbiter -->|"escalate"| Human["Human gate"]

    Ledger["Trajectory ledger\nPhaseEvents + Artifacts + checkpoints"] -. records .-> Policy
    Ledger -. records .-> Reviewers
    Ledger -. records .-> Arbiter
```

This framing exposes several research objects that a conventional exam generator does not preserve:

- **verifier outputs:** pass/fail, severity, finding code, target, rationale, and evidence;
- **verifier disagreement:** conflicting reports over the same immutable content versions;
- **process supervision:** which role failed, what feedback was issued, and which local action followed;
- **reward candidates:** deterministic validity signals and structured verifier judgments that can later be calibrated into rewards;
- **replayable trajectories:** exact inputs, outputs, versions, model-call metadata, state transitions, and recovery events;
- **counterfactual repair points:** problem, solution, rubric, question plan, section, or full-run boundaries.

## Workbench

The local React workbench exposes the complete run instead of hiding it behind a final PDF. A completed 19-question run can be inspected question by question, edited, rerun, and published from the same interface.

![Question workspace showing a completed 19-question run](docs/assets/demo/workbench-questions.png)

The document workspace keeps the student paper, worked solutions, and scoring rubric together, with page counts, build status, inline PDF preview, and direct downloads.

![Three-view PDF document workspace](docs/assets/demo/workbench-documents.png)

The overview retains phase history, recovery events, child-run states, and final completion counters for audit and debugging.

![Completed workflow overview with phase and child-run history](docs/assets/demo/workbench-overview.png)

| UI acceptance-run signal | Observed value |
| --- | ---: |
| Completed questions | **19 / 19** |
| Parallel subject-research roles | **3** |
| Published document views | **3 / 3** |
| Recorded phase events | **59** |
| Isolated child runs | **65** |

The interface screenshots use a completed dynamic discrete-mathematics workspace. The downloadable release below is a separate Gaokao mathematics case study. Both are preserved local acceptance runs, not mocked UI data.

## Verified Demo

The repository includes a real end-to-end release produced by the workbench: a 19-question, 150-point Chinese Gaokao mathematics mock exam.

<table>
  <tr>
    <td width="33%" align="center"><strong>Student paper</strong></td>
    <td width="33%" align="center"><strong>Worked solutions</strong></td>
    <td width="33%" align="center"><strong>Scoring rubric</strong></td>
  </tr>
  <tr>
    <td><img src="docs/assets/demo/exam-questions.png" alt="Rendered student exam page"></td>
    <td><img src="docs/assets/demo/exam-solutions.png" alt="Rendered solution page"></td>
    <td><img src="docs/assets/demo/exam-rubric.png" alt="Rendered rubric page"></td>
  </tr>
</table>

| Verified property | Observed result |
| --- | ---: |
| Questions / total score | 19 / 150 |
| Published views | student paper, solutions, rubric |
| Rendered pages | 5 + 16 + 13 = **34** |
| Full-page render inspections | **3 / 3 passed** |
| Blocking render findings | **0** |
| Slowest parallel document build | **24.2 s** |
| Release status | document gate approved |

Download the actual artifacts:

- [Student paper](examples/gaokao-mathematics/artifacts/exam-questions.pdf)
- [Worked solutions](examples/gaokao-mathematics/artifacts/exam-solutions.pdf)
- [Scoring rubric](examples/gaokao-mathematics/artifacts/exam-rubric.pdf)
- [Demo provenance and limitations](examples/gaokao-mathematics/README.md)

These are acceptance-run measurements, not a multi-seed benchmark. Mathematical correctness has not yet been independently expert-rated; the current evidence establishes workflow completion, artifact integrity, and render quality.

## Design Principles

1. **Reasoning is separated from control.** Agents propose typed outputs; the runtime decides whether those outputs can advance the workflow.
2. **JSON domain objects are the source of truth.** Markdown, LaTeX, PDF, logs, and page images are rebuildable projections.
3. **Every expensive stage has an Artifact boundary.** A completed stage can be reused without repeating its model call.
4. **Failures are localized.** Questions, reviewers, and document views run as isolated children with independent retry histories.
5. **Review is independent and version-bound.** A report is reusable only when its question, solution, and rubric version IDs still match.
6. **Human gates are explicit state transitions.** Approval, edit-acceptance, retry, rejection, and abort are recorded as decisions.
7. **Provider details stay behind ports.** The domain layer does not depend on a specific model provider, Agent framework, parser, vector database, or RAG product.

## System Architecture

```mermaid
flowchart TB
    subgraph Interfaces["Interfaces"]
        CLI["Typer CLI"]
        GUI["React Workbench"]
        API["FastAPI + SSE"]
    end

    subgraph Application["Application Layer"]
        Service["WorkbenchApplicationService"]
        Commands["Run, edit, approve, retry, resume"]
    end

    subgraph Orchestration["Deterministic Orchestration"]
        Engine["WorkflowEngine"]
        ExamFlow["ExamAgentWorkflow"]
        QuestionFlow["QuestionAgentWorkflow"]
        ReviewPools["Reviewer Pool Workflows"]
        DocumentFlow["Document Build Workflows"]
    end

    subgraph Reasoning["Reasoning Roles"]
        Research["Subject Researchers + Synthesizer"]
        Planner["Blueprint + Question Planner"]
        Writer["Question Writer"]
        Solver["Independent Solver"]
        Rubric["Rubric Builder"]
        Reviewers["Question and Exam Reviewers"]
        Arbiter["Question and Exam Arbiters"]
    end

    subgraph Domain["Domain and Contracts"]
        Models["Pydantic domain models"]
        Registries["Prompt + Capability Registries"]
        Validators["Deterministic validators"]
    end

    subgraph Persistence["Persistence and Audit"]
        RunStore["SQLite RunStore\nRuns, events, checkpoints, decisions"]
        ArtifactStore["ArtifactStore\nVersioned JSON, PDF, logs, page images"]
        Editable["Editable projections\nCAS-protected question versions"]
    end

    subgraph Adapters["External Adapters"]
        ModelsAPI["OpenAI-compatible model APIs"]
        Parsers["Fixture / MinerU parsers"]
        Latex["LaTeX + Tectonic"]
        Poppler["Poppler PDF inspection"]
    end

    CLI --> Service
    GUI --> API --> Service
    Service --> Commands --> ExamFlow
    ExamFlow --> Engine
    ExamFlow --> QuestionFlow
    ExamFlow --> ReviewPools
    ExamFlow --> DocumentFlow
    Engine --> RunStore
    QuestionFlow --> ArtifactStore
    ReviewPools --> ArtifactStore
    DocumentFlow --> ArtifactStore
    Service --> Editable
    Research --> ModelsAPI
    Planner --> ModelsAPI
    Writer --> ModelsAPI
    Solver --> ModelsAPI
    Rubric --> ModelsAPI
    Reviewers --> ModelsAPI
    Arbiter --> ModelsAPI
    ExamFlow --> Research
    ExamFlow --> Planner
    QuestionFlow --> Writer
    QuestionFlow --> Solver
    QuestionFlow --> Rubric
    ReviewPools --> Reviewers
    ExamFlow --> Arbiter
    Models --> Engine
    Registries --> ExamFlow
    Validators --> ExamFlow
    Parsers --> Service
    DocumentFlow --> Latex --> Poppler
```

The architecture deliberately avoids making an Agent framework the system of record. Agent outputs become useful only after they validate against domain contracts and are committed as versioned Artifacts.

## Run Hierarchy

One exam is a tree of independently observable runs rather than a single long coroutine.

```mermaid
flowchart TB
    Parent["Exam parent run\nexam_agent_generation"]

    Parent --> ResearchA["Subject research child\ncurriculum"]
    Parent --> ResearchB["Subject research child\nassessment design"]
    Parent --> ResearchC["Subject research child\nquality policy"]

    Parent --> Q1["Question child 01\nexam_question_generation"]
    Parent --> QN["Question child N\nexam_question_generation"]

    Q1 --> QR1["Reviewer grandchild\nmathematical"]
    Q1 --> QR2["Reviewer grandchild\nsolvability"]
    Q1 --> QR3["Reviewer grandchild\npedagogical"]
    Q1 --> QR4["Reviewer grandchild\nsubject / rubric / structure"]

    Parent --> ER1["Exam review child\nduplication"]
    Parent --> ER2["Exam review child\nconsistency"]
    Parent --> ER3["Exam review child\nleakage / risk"]

    Parent --> DQ["Document child\nstudent paper"]
    Parent --> DS["Document child\nsolutions"]
    Parent --> DR["Document child\nrubric"]
```

This hierarchy provides separate status, events, checkpoints, attempts, and Artifacts for each expensive unit of work. A failed reviewer does not erase successful sibling reports; a failed PDF view does not invalidate the other two views.

## End-to-End Exam Workflow

The parent workflow uses 15 named phases. Dynamic subjects execute the research branch; explicit presets and registered capabilities may reuse locked structures while still entering the same downstream pipeline.

```mermaid
flowchart TD
    Request["Course evidence + exam request"] --> Resolve{"Planning mode"}
    Resolve -->|"Explicit profile + blueprint"| Preset["Validate preset contracts"]
    Resolve -->|"Registered subject capability"| Capability["Load locked structure and policies"]
    Resolve -->|"Unregistered subject"| Research["SUBJECT_RESEARCHING\nparallel research children"]
    Research --> Synthesis["SUBJECT_SYNTHESIZING"]
    Synthesis --> PlanExam["EXAM_PLANNING"]
    Preset --> PlanExam
    Capability --> PlanExam
    PlanExam --> BlueprintGate["BLUEPRINT_APPROVAL"]
    BlueprintGate --> PlanQuestions["QUESTION_PLANNING"]
    PlanQuestions --> RevisePlans["QUESTION_PLANS_REVISING"]
    RevisePlans --> Generate["QUESTIONS_GENERATING\nparallel question children"]
    Generate --> Assemble["EXAM_ASSEMBLING"]
    Assemble --> ExamReviews["EXAM_REVIEWS_GENERATING\nparallel exam reviewers"]
    ExamReviews --> ExamArbiter["EXAM_ARBITRATING"]
    ExamArbiter -->|"replace questions / regenerate section"| Generate
    ExamArbiter -->|"rebalance coverage / difficulty"| RevisePlans
    ExamArbiter -->|"pass / warning / escalate"| Finalize["EXAM_FINALIZING"]
    Finalize --> ExamGate["EXAM_APPROVAL"]
    ExamGate --> Documents["DOCUMENTS_BUILDING\n3 parallel document children"]
    Documents --> DocumentGate["DOCUMENT_APPROVAL"]
    DocumentGate --> Release["RELEASE_BUNDLING"]
    Release --> Done["DONE"]
```

## Agent Interaction

```mermaid
sequenceDiagram
    autonumber
    actor User
    participant Parent as Exam Parent Workflow
    participant Research as Research Pool
    participant Planner as Planner
    participant Questions as Question Children
    participant Writer as Writer
    participant Solver as Independent Solver
    participant Rubric as Rubric Builder
    participant Reviewers as Reviewer Pool
    participant QArbiter as Question Arbiter
    participant EReviewers as Exam Reviewer Pool
    participant EArbiter as Exam Arbiter
    participant Documents as 3 Document Children
    participant Stores as RunStore + ArtifactStore

    User->>Parent: subject, level, constraints, optional sources
    alt unregistered subject
        par independent research roles
            Parent->>Research: curriculum scope
            Parent->>Research: assessment design
            Parent->>Research: quality policy
        end
        Research-->>Stores: reports + child-run manifest
        Parent->>Research: synthesize typed profile and blueprint
    else registered capability or explicit preset
        Parent->>Stores: validated profile and blueprint
    end

    Parent->>Planner: generate typed question plans
    Planner-->>Stores: raw draft, validation progress, final plans

    par one isolated child per question
        Parent->>Questions: question plan + locked contracts
        Questions->>Writer: generate question only
        Writer-->>Stores: QuestionVersion
        Questions->>Solver: solve without reusing writer rationale
        Solver-->>Stores: SolutionVersion
        Questions->>Rubric: build scoring rules from question + solution
        Rubric-->>Stores: RubricVersion
        par independent reviewer grandchildren
            Questions->>Reviewers: mathematical review
            Questions->>Reviewers: solvability review
            Questions->>Reviewers: pedagogy / subject / rubric review
        end
        Reviewers-->>Stores: version-bound reports
        Questions->>QArbiter: bundle + reports
        QArbiter-->>Questions: pass or targeted retry
        Questions-->>Stores: accepted ExamQuestionBundle
    end

    Parent->>EReviewers: assembled exam + all version IDs
    EReviewers-->>Stores: duplication, consistency, leakage, risk reports
    Parent->>EArbiter: exam + reports
    EArbiter-->>Parent: pass or targeted local repair
    Parent->>Documents: approved ExamDocument
    par independent views
        Documents->>Documents: render student paper
        Documents->>Documents: render solutions
        Documents->>Documents: render rubric
    end
    Documents-->>Stores: TeX, PDF, logs, page PNGs, inspection reports
    Parent-->>User: auditable release bundle
```

The Writer, Solver, and Rubric Builder do not share an unconstrained conversational transcript. They communicate through typed, versioned domain objects. Reviewers bind to exact version IDs, which prevents a report from being silently reused after an edit.

## Structured Feedback and Reward Candidates

The system does not currently collapse evaluation into one scalar reward. It preserves a richer signal vector that can be replayed, audited, and calibrated later.

| Signal family | Existing source | Example interpretation |
| --- | --- | --- |
| Contract validity | Pydantic and deterministic validators | hard negative when required fields, score totals, slot contracts, or version bindings fail |
| Independent solution evidence | `SolutionVersion` plus solvability/mathematical reviews | semantic correctness candidate independent of Writer self-evaluation |
| Rubric consistency | `RubricVersion` plus rubric Reviewer | whether scoring rules agree with the question and reference solution |
| Verifier ensemble | version-bound `ReviewReport` objects | pass/fail vector, severity distribution, finding codes, target-specific feedback |
| Verifier disagreement | reports over the same Bundle signature | uncertainty signal or trigger for stronger verification / human review |
| Arbitration action | `PASS`, targeted retry, escalation, or abort | structured process-level supervision rather than free-text critique alone |
| Whole-exam checks | coverage, difficulty, duplication, leakage, consistency | global constraint reward candidates unavailable at single-question scope |
| Reliability signals | retries, interruptions, recovery events, duplicate calls | efficiency and robustness penalties for Agentic RL environments |
| Publication gates | compile status, page inspection, human acceptance | executable final-state validity signal |

A future experiment can derive a calibrated reward without discarding the original evidence, for example:

```text
reward_candidate =
    contract_validity
  + independent_solution_score
  + rubric_consistency
  + verifier_consensus
  + coverage_gain
  - blocking_findings
  - duplicate_penalty
  - recovery_cost
```

This expression is a proposed research interface, not a currently trained reward model. The repository stores the components needed to test alternative weighting, aggregation, disagreement handling, and anti-hacking rules offline.

## Workflow Run State Machine

`WorkflowRun.status` is validated against an explicit transition table.

```mermaid
stateDiagram-v2
    [*] --> Queued
    Queued --> Running
    Queued --> Cancelled

    Running --> WaitingHuman: human gate
    Running --> Succeeded: all phases complete
    Running --> Interrupted: transient error or process stop
    Running --> Failed: permanent error
    Running --> Cancelling: cancellation requested

    WaitingHuman --> Running: allowed service transition
    WaitingHuman --> Interrupted: accept, edit-accept, or retry stores resume point
    WaitingHuman --> Failed: reject
    WaitingHuman --> Cancelled: abort
    WaitingHuman --> Cancelling

    Interrupted --> Running: resume from checkpoint
    Interrupted --> Cancelling
    Interrupted --> Cancelled
    Interrupted --> Failed

    Failed --> Interrupted: audited retry-failed recovery

    Cancelling --> Cancelled
    Cancelling --> Interrupted
    Cancelling --> Failed

    Succeeded --> [*]
    Failed --> [*]
    Cancelled --> [*]
```

Each named phase emits a paired `running` and `completed` event sharing an occurrence ID. Failures emit a `failed` event with an error code and details. The event round increases whenever the same phase is re-entered.

## Question State Machine and Local Retry

Every question has its own run, persistent `QuestionWorkflowState`, retry counters, and version chain.

```mermaid
stateDiagram-v2
    [*] --> Initializing
    Initializing --> ProblemGenerating
    ProblemGenerating --> SolutionGenerating: QuestionVersion committed
    SolutionGenerating --> RubricGenerating: SolutionVersion committed
    RubricGenerating --> ReviewsGenerating: RubricVersion committed
    ReviewsGenerating --> Arbitrating: all required reports available
    Arbitrating --> Finalizing: pass or pass with warnings
    Arbitrating --> Finalizing: escalate human or retry budget exhausted
    Arbitrating --> ProblemGenerating: retry_problem or retry_all
    Arbitrating --> SolutionGenerating: retry_solution
    Arbitrating --> RubricGenerating: retry_rubric
    Arbitrating --> ReviewsGenerating: reports missing or version mismatch
    Finalizing --> [*]: ExamQuestionBundle committed
```

Arbitration feedback is routed only to the responsible role. Retrying a solution keeps the accepted question version; retrying a rubric keeps both question and solution versions. Exhausted local budgets finalize the latest Bundle with `requires_human_review=true` instead of looping indefinitely.

## Whole-Exam Review and Repair

Question-level validity is necessary but not sufficient. The assembled exam is checked for cross-question properties and can repair only the affected region.

```mermaid
flowchart TD
    Assemble["Assemble ExamDocument"] --> Deterministic["Deterministic checks\nscore closure, slot structure, coverage, duration"]
    Deterministic --> Reviews["Parallel exam reviewers\nduplication, consistency, leakage, source risk"]
    Reviews --> Gate{"Blocking findings?"}
    Gate -->|"no"| Pass["PASS or PASS_WITH_WARNINGS"]
    Gate -->|"yes, question targets"| Replace["REPLACE_QUESTIONS"]
    Gate -->|"yes, section targets"| Section["REGENERATE_SECTION"]
    Gate -->|"coverage targets"| Coverage["REBALANCE_COVERAGE"]
    Gate -->|"difficulty targets"| Difficulty["REBALANCE_DIFFICULTY"]
    Gate -->|"no safe target"| Human["ESCALATE_HUMAN"]
    Replace --> Regenerate["Regenerate only selected question children"]
    Section --> Regenerate
    Coverage --> Replan["Revise only selected QuestionPlans"]
    Difficulty --> Replan
    Replan --> Regenerate
    Regenerate --> Assemble
    Pass --> Final["Finalize exam"]
    Human --> Final
```

Replacement history preserves the old child-run pointer and Bundle. Non-target questions remain unchanged. Review reports are invalidated whenever any bound question, solution, or rubric version changes.

## Checkpoint and Recovery Design

```mermaid
flowchart LR
    Start["Enter named phase"] --> RunningEvent["Append running PhaseEvent"]
    RunningEvent --> Work["Execute deterministic or Agent step"]
    Work --> Artifacts["Write immutable Artifacts"]
    Artifacts --> Commit["Single SQLite transaction:\ncompleted PhaseEvent + checkpoint"]
    Commit --> Next["Advance next_step_index"]

    Work -->|"transient provider error / process stop"| Interrupted["Run = interrupted"]
    Interrupted --> Resume["Load checkpoint context, Artifact bindings, child-run IDs"]
    Resume --> Reuse["Reuse completed stages and matching successful children"]
    Reuse --> Work

    Work -->|"permanent validation or code error"| Failed["Run = failed"]
    Failed -->|"audited retry-failed whitelist"| RecoveryEvent["Append RUN_RECOVERY event"]
    RecoveryEvent --> Interrupted
```

`WorkflowCheckpoint` stores the next step index, scalar context, Artifact bindings, child-run IDs, and the latest human decision ID. Large typed objects are restored from Artifact IDs rather than serialized into the checkpoint.

The completed phase event and checkpoint are committed in one SQLite transaction. Artifact files and SQLite are different transaction domains, so publication uses recoverable write-and-bind semantics rather than claiming cross-media ACID.

## Replayable Trajectories for Agentic RL

The runtime records enough structure to reconstruct an Agent episode without relying on a single concatenated chat log.

```mermaid
flowchart LR
    Observation["Observation\nrequest + source context + bound Artifacts"] --> Action["Agent action\ntyped model output"]
    Action --> Validation["Environment transition\nSchema + deterministic validation"]
    Validation --> Verifiers["Verifier observations\nreports + findings + disagreement"]
    Verifiers --> Decision["Arbiter action\npass, targeted retry, escalation"]
    Decision --> Next["Next observation\nnew versions + feedback + counters"]
    Next --> Action

    Observation --> Trace["Replay record"]
    Action --> Trace
    Validation --> Trace
    Verifiers --> Trace
    Decision --> Trace
    Trace --> Dataset["Future trajectory / preference / RLVR dataset exporter"]
```

An episode can include:

- the exact Prompt version, response Schema hash, ContextPack hash, request hash sequence, and response hash;
- typed observations referencing immutable input Artifact versions;
- Agent outputs and deterministic validation failures;
- parallel verifier reports and their completion order;
- Arbiter decisions, role-specific feedback, and targeted retry actions;
- checkpoint boundaries, interruption/recovery events, latency, and token usage;
- final Bundle and publication-gate outcome.

Today these records support audit, resume, and local replay. A dedicated dataset exporter and policy-training loop are future work; the infrastructure should therefore be described as **Agentic RL-ready trajectory infrastructure**, not as a completed Agentic RL system.

## Document Build and Publication

```mermaid
flowchart TB
    Exam["Approved ExamDocument"] --> Batch["DocumentBatchWorkflow"]
    Batch --> Q["Student-paper child"]
    Batch --> S["Solutions child"]
    Batch --> R["Rubric child"]

    Q --> QR["DOCUMENT_RENDERING"] --> QC["PDF_COMPILING"] --> QI["PDF_INSPECTING"]
    S --> SR["DOCUMENT_RENDERING"] --> SC["PDF_COMPILING"] --> SI["PDF_INSPECTING"]
    R --> RR["DOCUMENT_RENDERING"] --> RC["PDF_COMPILING"] --> RI["PDF_INSPECTING"]

    QI --> Gate["Document approval gate"]
    SI --> Gate
    RI --> Gate
    Gate --> Bundle["Release Bundle\ncontent IDs + model audit + reviews + PDFs + pages"]
```

Each view independently produces LaTeX source, compiler log, PDF, page PNGs, and a machine-readable inspection report. Page inspection checks text extraction, ink ratios, edge content, empty pages, and blocking render findings. Only failed views need rebuilding.

## Planning Modes and Registries

Planning resolution uses the following priority:

1. explicit `SubjectProfile` and `ExamBlueprint` supplied by the caller;
2. a registered `SubjectCapability` such as the 19-question Gaokao mathematics structure;
3. dynamic subject research and synthesis for unregistered subjects.

```text
PromptRegistry
  -> PromptBundle(key, role, version, system_prompt)

CapabilityCatalog
  -> ReviewerRegistry
  -> SubjectResearchRegistry
  -> ToolRegistry
  -> ValidatorRegistry
  -> SubjectCapabilityRegistry
```

Capabilities lock structure and policies, not static questions. Prompt versions, capability IDs, validator names, model roles, request hashes, response hashes, token usage, and provider request IDs are written into the audit trail.

## Artifact and Audit Model

| Record | Purpose |
| --- | --- |
| `WorkflowRun` | Current workflow status, phase, owner process, error |
| `PhaseEvent` | Immutable phase occurrence with parent linkage, inputs, outputs, timing, warnings, and errors |
| `WorkflowCheckpoint` | Resume index plus Artifact and child-run bindings |
| `ArtifactRef` | Versioned logical name, path, media type, SHA-256, size, producing phase |
| `ModelCall` | Role, model, prompt version, schema/context hashes, request sequence, usage, provider metadata |
| `HumanReviewRequest` | Gate prompt, allowed decisions, resume phase, retry phase, bound Artifacts |
| `HumanDecision` | Actor, decision, reason, input Artifact IDs, timestamp |
| `QuestionVersion` / `SolutionVersion` / `RubricVersion` | Independently versioned assessment content with parent-version links |
| `ReviewerRunRecord` | Reviewer attempt bound to exact content version IDs |
| `ReleaseBundle` | Final content signature, run graph, model audit, reviews, arbitration, documents, logs, pages, acceptance |

## Quick Start

```bash
git clone git@github.com:kyc001/assessment-workbench.git
cd assessment-workbench
uv sync
cp .env.example .env

uv run assessment-workbench workspace init ./workspaces/demo
uv run assessment-workbench gui --workspace ./workspaces/demo
```

Generate a full exam:

```bash
uv run assessment-workbench exams generate \
  --subject "高考数学" \
  --target-level "高中毕业年级" \
  --requirements "19 题，150 分，标准模拟卷" \
  --workspace ./workspaces/demo
```

Human-gated runs pause before release:

```bash
uv run assessment-workbench runs approve <run-id> --workspace ./workspaces/demo
uv run assessment-workbench runs resume <run-id> --workspace ./workspaces/demo
```

Resume a transiently interrupted run:

```bash
uv run assessment-workbench runs resume <run-id> --workspace ./workspaces/demo
```

## Repository Map

```text
src/assessment_workbench/
  domain.py                    typed domain models and transition contracts
  workflow.py                  generic checkpointed workflow engine
  agents.py                    parent exam orchestration
  question_workflow.py         Writer / Solver / Rubric / review / arbitration loop
  review_workflow.py           isolated question reviewer children
  exam_review_workflow.py      isolated whole-exam reviewer children
  exam_workflow.py             exam-level review gates and targeted repair routing
  document_workflow.py         LaTeX, PDF compilation, inspection, page Artifacts
  storage.py                   SQLite RunStore and filesystem ArtifactStore
  web_api.py                   typed local HTTP and SSE interface

frontend/                      React local workbench
tests/                         offline unit and integration tests
examples/                      constraints and published demo artifacts
docs/                          architecture and implementation notes
```

Further reading:

- [Architecture notes](docs/architecture.md)
- [Gaokao demo](examples/gaokao-mathematics/README.md)
- [Implementation status](docs/IMPLEMENTATION_PLAN.md)

## Reward-Hacking Threat Model

Assessment generation is useful for verifier research because outputs can appear structurally correct while exploiting weaknesses in semantic checks or scoring rules.

| Attack family | Adversarial candidate | Expected defense signal |
| --- | --- | --- |
| Format compliance without semantic validity | valid JSON and polished LaTeX, but ambiguous or unsolvable question | independent Solver failure, solvability findings |
| Lucky final answer | correct final value with invalid reasoning | mathematical Reviewer checks steps, not only answer string |
| Self-consistent fabrication | Writer, solution, and rubric repeat the same false premise | role isolation plus subject/mathematical verification |
| Rubric gaming | answer exploits missing scoring conditions or receives points without required reasoning | rubric consistency and adversarial scoring review |
| Verifier persuasion | verbose rationale attempts to override blocking evidence | deterministic gate forbids `PASS` while error/fatal findings remain |
| Citation laundering | plausible source claim without matching source block | source-reference and grounding validation |
| Duplicate camouflage | superficial wording changes hide repeated constructions | whole-exam duplication review over the assembled exam |
| Difficulty gaming | trivial or impossible questions satisfy nominal metadata | solver-based calibration and whole-exam difficulty checks |
| Recovery exploitation | retries mutate unrelated accepted content or replay expensive calls | immutable versions, target resolution, checkpoint and replacement history |

The infrastructure already records the evidence needed to measure attack success, verifier recall, false positives, disagreement, and repair cost. It does **not** yet include a published adversarial benchmark or a measured reduction in reward-hacking attack success rate.

## RLVR and Reward-Hacking Evaluation Roadmap

The highest-value next experiment is a controlled verifier and adversarial-evaluation pilot:

1. Freeze course evidence, model versions, schemas, prompts, budgets, and random seeds.
2. Build clean and adversarial response pairs covering format-valid/semantically-wrong answers, lucky answers with invalid reasoning, shared false premises, and rubric loopholes.
3. Compare deterministic checks, individual Verifiers, verifier ensembles, and Arbiter-gated decisions.
4. Report attack success rate, verifier recall/precision, disagreement rate, false-rejection rate, repair success, and cost per accepted valid question.
5. Replay the same trajectories under alternative reward aggregation rules without repeating model generation.
6. Compare Single Agent, Fixed Pipeline, and the role-separated workflow under equal-budget and natural-run settings.

Only after this experiment should the project claim statements such as “reduced reward-hacking attack success by XX%” or “improved verifier recall by XX points.” Until then, the accurate positioning is **verifier-centric evaluation, structured reward candidates, and replayable trajectory infrastructure for RLVR/Agentic RL**.

## Development

```bash
uv run ruff check .
uv run mypy
uv run pytest
npm --prefix frontend run typecheck
npm --prefix frontend run build
```

## License

Apache-2.0. Generated assessment artifacts remain subject to the provenance and licensing of their source materials.
