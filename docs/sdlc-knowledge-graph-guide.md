# Developer Guide: Aerospace SDLC Knowledge Graph Architecture

## 1. Overview

This guide describes a two-tier knowledge graph architecture for aerospace and regulated software lifecycle management. It combines a semantic ontology tier (Apache Fuseki) with a traversal-optimised operational graph tier (Neo4j), orchestrated via pipeline automation to enable ALM-independent traceability, validation, and assurance.

The pattern separates semantic governance from operational execution. ALM, PLM, Git, CI/CD, test, and evidence systems remain the systems of record for their native artefacts, while the knowledge graph provides a governed integration, validation, traceability, and assurance layer across them.

## 2. Core Concepts

The system separates meaning from execution:

- **Semantic Tier (Fuseki):** defines ontology, rules, and constraints.
- **Operational Tier (Neo4j):** stores an operational projection of lifecycle entities and relationships optimised for traversal, impact analysis, pipeline queries, agent workflows, and dashboarding.
- **Orchestrator:** bridges ALM tools and graphs, enforcing rules in pipelines.

## 3. Node Types

Nodes represent heterogeneous lifecycle entities. In the ontology they form a class hierarchy rooted at a common `LifecycleEntity` superclass, grouped as:

- **Requirements:** System, Software, Safety, Interface
- **Architecture & code:** Design Element, Interface Specification, Software Component, Code Module
- **Verification artefacts:** Test Case, Test Procedure, Test Result, Review Record, Static Analysis Result
- **Validation artefacts:** Validation Record — confirms a requirement is the *right* requirement (matches stakeholder/operational need), as distinct from verification
- **Assurance artefacts:** Evidence, Certification Objective, Compliance Claim, Assurance Case
- **Safety:** Hazard, Safety Control
- **Work management & CI:** Work Item, Change Request, Defect, Pull Request, Pipeline Run
- **Configuration & release:** Configuration Item, Release Baseline
- **Actors:** Role, Team, Person

These node types are deliberately shaped around the **V-model** that ARP4754A codifies for aircraft/systems development:

- **Left leg (decomposition):** System Requirement → Software/Interface Requirement (`derivesFrom`) → Architecture Element/Software Component (`satisfies`/`implements`).
- **Base (implementation):** Code Module.
- **Right leg (integration climbing back up):** Test Case/Test Result/Review Record/Static Analysis Result (`verifies`), at both the software-requirement level and, mirroring the left leg, the system-requirement level (system integration testing).
- **Apex (validation):** Validation Record (`validates`) confirms the top-level System Requirement against operational need — this is the ARP4754A validation activity, and it is the "closing" edge of the V, distinct from and complementary to verification.
- **Safety thread (runs alongside the whole V, per ARP4761):** Hazard → Safety Control (`mitigates`), plus `derivesAssuranceLevel` from Hazard to the Requirement whose DAL that hazard's failure-condition classification determines.

Lifecycle state (e.g. Draft, Approved, Baselined) is **not** a node type — it is carried as a `status` property on each entity, alongside other shared properties such as `identifier`, `title`, `assuranceLevel` (e.g. DAL-A), `criticality`, and `version`.

These categories should be understood as ontology concepts first, then projected into operational graph labels. The two tiers represent them differently:

| Semantic tier | Operational tier |
| --- | --- |
| OWL classes, SHACL shapes, controlled vocabularies, process ontology, artefact ontology | Neo4j labels, relationship types, indexed properties, traversal-optimised projections |
| Requirement, ArchitectureElement, VerificationArtifact, AssuranceArtifact, Hazard, ReleaseBaseline, Actor | SystemRequirement, SoftwareRequirement, DesignElement, CodeModule, TestCase, Evidence, ComplianceClaim, PipelineRun, ReleaseBaseline |
| derivesFrom, satisfies, allocatedTo, verifiedBy, usesEvidence, mitigates | DERIVES_FROM, SATISFIES, ALLOCATED_TO, VERIFIES, USES_EVIDENCE, MITIGATES |

Note the naming convention: ontology classes and object properties use PascalCase / camelCase (`verifiedBy`), while their operational projections use Neo4j label and `SNAKE_CASE` relationship conventions (`VERIFIES`). This label-and-relationship mapping is exactly what the synchronisation service's mapping definitions encode (see [Section 12.6](#126-projection-and-mapping)).

## 4. Edge Semantics

Relationships encode meaning. The core traceability and assurance edges are:

- **DERIVES_FROM:** requirement decomposition (requirement → requirement)
- **SATISFIES:** an entity satisfies a requirement
- **ALLOCATED_TO:** a requirement is allocated to an architecture element
- **IMPLEMENTS / IMPLEMENTED_BY:** code-to-requirement (inverse pair)
- **VERIFIES / VERIFIED_BY:** verification artefact to requirement (inverse pair)
- **VALIDATES:** a validation record confirms a requirement against stakeholder/operational need — the apex-of-the-V edge, complementary to (not a substitute for) `VERIFIES`
- **PRODUCES:** activity / entity outputs
- **REVIEWED_BY:** an entity is reviewed by a review record
- **USES_EVIDENCE:** a compliance claim draws on evidence
- **CLAIMS_COMPLIANCE_WITH:** a compliance claim addresses a certification objective
- **MITIGATES:** a safety control mitigates a hazard
- **TRACES_TO_HAZARD:** a requirement traces to a hazard
- **DERIVES_ASSURANCE_LEVEL:** a hazard's failure-condition classification is the traceable source of a requirement's assigned Development Assurance Level (DAL), per ARP4754A/ARP4761 — without this edge, `assuranceLevel` is an unexplained property rather than a derived, auditable value
- **AFFECTED_BY / CHANGES:** an entity is affected by a change request
- **RESOLVES:** a change request resolves a defect
- **OWNED_BY / ASSIGNED_TO:** ownership and work assignment to actors
- **INCLUDED_IN_BASELINE / HAS_CONFIGURATION_ITEM:** configuration and release membership
- **EVALUATED_IN:** an entity is evaluated in a pipeline run
- **SUPERSEDES:** versioning

The ontology defines several of these as inverse pairs (e.g. `implements`/`implementedBy`, `verifies`/`verifiedBy`); the operational projection typically materialises a single canonical direction. The operational graph may also carry relationship types that have **no** ontology object property — for example execution, change-impact, or interface-usage edges used purely for traversal. Those are legitimate operational projections, but they are precisely what drift detection surfaces as *unmapped operational relationships* (see [Section 12.5](#125-core-capabilities)), so they should be deliberately either mapped into the ontology or recorded as intentionally operational-only.

## 5. Traceability Chains

Core value is end-to-end traceability:

SystemRequirement → SoftwareRequirement → Design → Code → Test → Evidence, with a ValidationRecord closing the loop back to SystemRequirement (`validates`) to confirm the top of the V was the right target in the first place.

A missing required relationship represents a potential assurance gap. Whether it is a compliance failure depends on lifecycle state, applicability, criticality, process rules, and baseline context.

A missing edge may represent:

- A genuine assurance gap
- A not-yet-applicable lifecycle state
- An incomplete draft
- A deliberately deferred obligation
- A mapping/configuration defect
- A source-system synchronisation delay
- A human review pending state

For ALM integration, provenance is not optional. Every fact should know where it came from, when it was imported, which mapping created it, and which version of the source artefact it represents.

Provenance metadata should capture concepts such as:

- Source system
- Source entity ID
- Source revision/version
- Ingestion run
- Mapping version
- Validation result
- Human review result
- Baseline membership
- Change event

Without this, the KG may support traceability but not trustworthy assurance. At the property level these concepts surface as fields such as `sourceSystem`, `externalId`, and `version` carried on every lifecycle entity, so each operational node remains attributable back to its system of record.

In our model, rules should be lifecycle-sensitive. Each entity's `status` and `assuranceLevel` (e.g. DAL-A) properties drive which obligations apply. For example:

- Draft requirements may not need full verification evidence.
- Baselined requirements may need allocation and review evidence.
- Implemented software requirements may need design/code/test links.
- Released software may need final verification, configuration, and compliance evidence.

So the architecture should distinguish **"required eventually"** from **"required at this lifecycle gate."**

## 6. Two-Tier Architecture

Fuseki defines the ontology using OWL and SHACL; Neo4j stores the instance graph. The orchestrator ensures the Neo4j data conforms to the Fuseki constraints.

The two tiers hold distinct authority and must not be collapsed:

- The **semantic tier** is the authority for meaning — ontology definitions, vocabularies, constraints, SHACL validation rules, semantic mappings, and validation artefacts. It answers *"what does this mean, what is valid, what is allowed, what constraints must hold?"*
- The **operational tier** is the authority for live engineering state — artefacts, their lifecycle and review status, workflow execution, provenance, traceability, and evidence metadata. It answers *"what artefacts exist, what state are they in, what produced them, what do they trace to, what evidence supports them?"*

The semantic tier is never used as the operational data store, and live operational instance data is never persisted into the semantic graph. Keeping the two tiers aligned is the job of the Semantic-to-Operational Graph Synchronisation Service (see [Section 12](#12-semantic-to-operational-graph-synchronisation-service)).

## 7. Orchestrator Responsibilities

- **Ingestion:** map ALM entities to ontology
- **Enrichment:** infer semantic relationships
- **Validation:** enforce SHACL constraints
- **Pipeline gating:** fail builds if assurance gaps exist

The orchestrator does not write to the operational graph directly. All operational writes — including validation and synchronisation outcomes — are submitted through the governed **Graph Ingestion Service**, the only component permitted to mutate the operational graph. Validation itself is delegated to the Semantic-to-Operational Graph Synchronisation Service, which the orchestrator (or a deterministic engineering service) invokes synchronously at each gate.

## 8. Assurance & Compliance

Standards and process obligations may represent objectives, activities, artefacts, reviews, evidence expectations, lifecycle gates, safety assessments, software assurance objectives, and customer-specific compliance rules.

Compliance is modelled as an explicit, traceable chain rather than a single flag: a **Compliance Claim** *claims compliance with* a **Certification Objective** (e.g. a DO-178C objective) and *uses* one or more **Evidence** artefacts produced by verification activities. Safety is modelled in parallel: a **Hazard** is mitigated by a **Safety Control**, and requirements may *trace to* the hazards they address. This makes both "which objective is satisfied, by what evidence" and "which hazard is controlled, by what" first-class, queryable traceability paths — and makes a missing claim, evidence link, or mitigation a detectable assurance gap.

## 9. ALM Independence

ALM tools are abstracted. ALM integrations should be treated as adapters around a canonical lifecycle model. The canonical graph model avoids tool-specific concepts. Tool-specific identifiers, URLs, revisions, and metadata are retained as provenance, not as primary ontology semantics.

Writes into the operational graph are funnelled through a single governed component — the **Graph Ingestion Service**. Source adapters, the orchestrator, and the synchronisation service all submit changes by calling its API; none connect to the operational graph database for write operations. This keeps ingestion governed, auditable, and provenance-stamped regardless of which ALM/PLM/Git/CI/test system the data originated from.

```
ALM / PLM / Git / CI / Test Tools
        ↓
Source Adapter
        ↓
Canonical Lifecycle Event / Fact Model
        ↓
Semantic Validation against Ontology + SHACL
        ↓
Operational Projection into Neo4j
        ↓
Queries, Gates, Dashboards, Agents, Assurance Views
```

## 10. Tradeoffs

**Benefits:**

- Unified traceability
- Strong compliance validation
- Flexible schema evolution

**Challenges:**

- Higher modelling complexity
- The orchestration and governed-write path (orchestrator, Graph Ingestion Service, synchronisation service) becomes a critical dependency
- Requires governance discipline

## 11. Mental Model

Nodes represent obligations and artefacts. Edges represent satisfaction or constraint relationships. A missing required edge signals a *potential* assurance gap — not automatically a compliance failure; whether it is one depends on lifecycle state, applicability, criticality, process rules, and baseline context (see [Section 5](#5-traceability-chains)).

## 12. Semantic-to-Operational Graph Synchronisation Service

The Semantic-to-Operational Graph Synchronisation Service is the named architectural component that keeps the two tiers aligned. It reads ontology, vocabulary, SHACL, mapping, and validation artefacts from the semantic tier; projects selected operational subgraphs into RDF or JSON-LD; validates them against SHACL shapes; detects drift between the two models; and writes the outcomes back into the operational graph **only** through the governed Graph Ingestion Service. It synchronises *meaning, validation, and semantic projection* between the tiers without making the semantic tier the operational data store.

It is invoked synchronously by the orchestrator or by deterministic engineering services, and exposes a versioned HTTP API.

### 12.1 Authority Boundaries

| Concern | Authoritative tier |
| --- | --- |
| Meaning, constraints, vocabularies, ontology definitions, SHACL rules, semantic mappings, validation artefacts | Semantic tier (Apache Jena Fuseki / RDF triplestore) |
| Live operational facts, artefact state, workflow execution, provenance, traceability, review status, evidence metadata | Operational tier (Neo4j / labelled property graph) |

These are the same authority boundaries introduced in [Section 6](#6-two-tier-architecture), with the technology bindings made explicit: the semantic tier owns meaning and constraints (including *how operational structures map to semantic ones*), while the operational tier owns live state (including *what has been reviewed or approved*). The synchronisation service moves semantic knowledge and validation outcomes between the two.

### 12.2 Responsibilities

- Retrieve ontology, SHACL, mapping, vocabulary, ruleset, and competency-query artefacts from the semantic tier.
- Select and read operational subgraphs (read-only) for validation.
- Project selected operational content into RDF / JSON-LD using semantic mappings.
- Execute SHACL validation against the projection.
- Detect drift between the semantic model and the operational model.
- Produce validation and synchronisation reports.
- Publish validation outcomes and synchronisation metadata to the operational graph **via the Graph Ingestion Service**.

### 12.3 Non-Goals

The service must never:

- Replace the operational graph, or store live operational instance data in the semantic graph.
- Write directly to the operational graph database.
- Mutate ontology or SHACL artefacts (that belongs to a separate semantic-governance process).
- Auto-approve artefacts or change lifecycle / review state — **validation success is not approval**.
- Act as a generic SPARQL or Cypher proxy, or run uncontrolled queries on behalf of untrusted clients.

### 12.4 Core Synchronisation Pattern

```
Semantic Tier
    ↓ retrieve ontology / SHACL / mapping artefacts
Operational Tier
    ↓ read selected subgraph (read-only)
Project operational subgraph → RDF / JSON-LD
    ↓
Run SHACL validation
    ↓
Generate validation report
    ↓
Submit result to Graph Ingestion Service
    ↓
Operational graph records validation outcome
```

All writes to operational state flow through the Graph Ingestion Service.

### 12.5 Core Capabilities

- **Semantic artefact retrieval** — `getOntology`, `getShapes`, `getVocabulary`, `getMapping`, `getRuleset`, each keyed by name + version.
- **Semantic version resolution** — resolve an `active` version from `(domain, ruleset, version, effectiveDate)` when an explicit version is not supplied.
- **Operational subgraph selection** — by node ID, artefact ID, candidate-set ID, baseline ID, workflow-run ID, semantic domain, Cypher template name, or saved projection profile.
- **RDF / JSON-LD projection** — label→RDF class, relationship→object property, property→datatype property, plus lifecycle and provenance metadata, namespace expansion, and stable URI generation.
- **SHACL validation** — pluggable engines (pySHACL, Apache Jena SHACL); outputs pass/fail, violations, warnings, focus nodes, result paths, source constraints, severity, message, and ruleset version.
- **Validation result publication** — emits `ValidationRun`, `ValidationFinding`, `ValidationRuleset`, `ValidationTarget`, `SourceReference`, and `ProvenanceRecord` records through the Graph Ingestion Service.
- **Drift detection** — flags unmapped labels/relationships/properties, semantic classes absent operationally, SHACL rules referencing unmapped fields, deprecated semantic types, and stale ruleset versions.
- **Semantic mapping registry** — a runtime cache of mappings (TTL expiry, version pinning, explicit refresh, checksum comparison, invalidation); the semantic tier remains the source of truth.
- **Competency query execution** — run semantic competency queries (SPARQL, or Cypher-template equivalents) for completeness, traceability, gap, evidence-coverage, and assurance-readiness checks, publishing results as validation findings.

### 12.6 Projection and Mapping

Mappings are defined in YAML or JSON and retrieved from the semantic tier. The examples below use an illustrative generic `requirements` vocabulary (`req:` / `trace:` / `gov:`) rather than the aerospace SDLC namespace from earlier sections, to show the mapping mechanics independently of any one domain:

```yaml
mappingId: requirements-neo4j-rdf
version: 1.0.0
semanticDomain: requirements

namespaces:
  req: https://example.org/ontology/requirements#
  gov: https://example.org/ontology/governance#
  trace: https://example.org/ontology/trace#

nodeMappings:
  Requirement:
    rdfClass: req:Requirement
    uriTemplate: "urn:kg:requirement:{externalId}"
    properties:
      externalId: req:requirementId
      text: req:requirementText
      lifecycleState: gov:lifecycleState
      approvalState: gov:approvalState

relationshipMappings:
  DERIVED_FROM:
    rdfProperty: trace:derivedFrom
  REFINES:
    rdfProperty: trace:refines
  VERIFIED_BY:
    rdfProperty: trace:verifiedBy
```

A projected operational requirement then serialises to RDF such as:

```turtle
@prefix req: <https://example.org/ontology/requirements#> .
@prefix gov: <https://example.org/ontology/governance#> .
@prefix trace: <https://example.org/ontology/trace#> .

<urn:kg:requirement:REQ-001>
    a req:Requirement ;
    req:requirementId "REQ-001" ;
    req:requirementText "The system shall validate brake command input before actuation." ;
    gov:lifecycleState "CANDIDATE" ;
    trace:derivedFrom <urn:kg:source:CONOPS-001> .
```

Large projections should be written to object storage and referenced by URI rather than returned inline.

### 12.7 Validation Publication Contract

Validation results are recorded as a small graph and submitted through the Graph Ingestion Service — never written to Neo4j directly:

```
ValidationRun  -[VALIDATED]->    ValidationTarget
ValidationRun  -[FOUND]->        ValidationFinding
ValidationRun  -[USED_RULESET]-> ValidationRuleset
```

Example ingestion payload:

```json
{
  "requestId": "uuid",
  "workflowId": "semantic-validation",
  "workflowRunId": "validation-001",
  "sourceSystem": "semantic-operational-sync-service",
  "sourceType": "DETERMINISTIC_SERVICE",
  "sourceName": "SemanticOperationalSyncService",
  "sourceVersion": "0.1.0",
  "semanticDomain": "governance",
  "writeMode": "VALIDATION_RESULT",
  "approvalState": "APPROVED",
  "nodes": [
    {
      "externalId": "validation-001",
      "labels": ["ValidationRun"],
      "semanticType": "gov:ValidationRun",
      "properties": {
        "status": "FAILED",
        "ruleset": "requirements-completeness",
        "rulesetVersion": "1.0.0"
      }
    }
  ],
  "relationships": []
}
```

`approvalState` here records the governance state of the *validation record itself*; it must not be used to auto-approve the artefacts under validation.

### 12.8 API Surface

All endpoints are synchronous and versioned under `/api/v1`.

| Method & path | Purpose |
| --- | --- |
| `GET /api/v1/health` | Liveness plus tier connectivity (semantic, operational, ingestion). |
| `POST /api/v1/semantic/artefacts/resolve` | Resolve ontology / SHACL / mapping / vocabulary artefacts and their active versions. |
| `POST /api/v1/projection` | Project a selected operational subgraph to RDF / JSON-LD. |
| `POST /api/v1/validate` | Project, run SHACL validation, and optionally publish the result. |
| `POST /api/v1/drift/detect` | Detect semantic-operational drift for a domain / scope. |
| `POST /api/v1/cache/refresh` | Invalidate and refresh a cached mapping. |

A `/validate` response carries the run status, ruleset version, violation/warning counts, per-finding detail (severity, focus node, result path, message), and whether the result was published to the operational graph.

### 12.9 Security Constraints

- Authenticated access to the semantic tier.
- **Read-only** access to the operational graph; **write** access only to the Graph Ingestion Service API.
- No arbitrary SPARQL update or Cypher write; no uncontrolled query execution from untrusted clients.
- Domain-level authorisation.

### 12.10 Implementation Blueprint

Representative configuration (environment variables):

```
SERVICE_NAME, SERVICE_VERSION
SEMANTIC_TIER_URL, SEMANTIC_TIER_AUTH_MODE
OPERATIONAL_GRAPH_URI, OPERATIONAL_GRAPH_USERNAME, OPERATIONAL_GRAPH_PASSWORD
GRAPH_INGESTION_SERVICE_URL, GRAPH_INGESTION_SERVICE_AUTH_MODE
DEFAULT_VALIDATION_ENGINE, MAPPING_CACHE_TTL_SECONDS, LOG_LEVEL
# optional: OBJECT_STORAGE_CONNECTION_STRING, APPLICATIONINSIGHTS_CONNECTION_STRING
```

The service is packaged as a containerised API with modules for semantic artefact access (`semantic/`), operational reads (`operational/`), projection (`projection/`), validation (`validation/`), drift detection (`drift/`), ingestion publishing (`publishing/`), and the mapping cache (`cache/`). Tests (pytest) cover artefact retrieval, mapping resolution, RDF/JSON-LD projection, SHACL pass/fail, report mapping, ingestion publishing, drift detection, cache refresh, stale-mapping and missing-mapping cases, and large-projection object-storage handling — with integration tests against local Fuseki and Neo4j containers and a mocked Graph Ingestion Service.

**Acceptance** is reached when the service can retrieve and version-resolve semantic artefacts, select and project operational subgraphs to RDF/JSON-LD, run SHACL validation, detect drift, and publish results through the Graph Ingestion Service — with versioned/cached mappings, validation reports that include focus node, result path, severity, message and ruleset version, no direct operational writes, automated tests, and containerised local + deployable operation.

### 12.11 Design Constraint

The semantic tier defines meaning and validation rules; the operational tier stores live engineering state. This service synchronises meaning, validation, and semantic projection between them — it must not collapse the distinction between the two tiers.

## 13. Glossary

- **Ontology:** formal definition of concepts
- **SHACL:** validation rules for RDF graphs
- **Traceability:** linking requirements to evidence
- **DAL:** Design Assurance Level
- **Orchestrator:** pipeline component linking ALM to graphs
- **ALM:** Application Lifecycle Management
- **RDF:** Resource Description Framework — the semantic tier's triple-based data model
- **JSON-LD:** a JSON-based serialisation of RDF
- **Fuseki:** Apache Jena RDF triplestore used as the semantic tier
- **Projection:** read-only transformation of an operational subgraph into RDF / JSON-LD for validation
- **Drift:** divergence between the semantic model and the operational graph model
- **Graph Ingestion Service:** the single governed component permitted to write to the operational graph
- **Competency query:** a SPARQL query expressing a question the ontology must be able to answer (e.g. completeness or traceability checks)
- **Candidate set / Baseline:** named selections of operational content used as projection / validation scopes

## Additional sections you could explore

The Semantic-to-Operational Graph Synchronisation Service ([Section 12](#12-semantic-to-operational-graph-synchronisation-service)) now covers the semantic-to-operational projection and validation-publication concerns. The remaining areas to expand are:

- 14. Canonical Lifecycle Model
- 15. Source System Adapters
- 16. Provenance and Baselines
- 17. Validation Results and Evidence Records
- 18. Lifecycle-Aware Assurance Gates
- 19. Agent and Automation Interaction Model
- 20. Governance, Versioning, and Change Control
