# AI in 5 Days — Assessment Agent Brief

## Overview
Build and publish an AI agent using Google's Agent Development Kit (ADK) and submit it as a **public, root-level GitHub repository** for automated evaluation.

- **Evaluator Portal**: https://fde-project-evaluator-510868799189.us-central1.run.app/
- **Login**: https://fde-project-evaluator-510868799189.us-central1.run.app/login

## Submission Checklist
- [ ] Formulate a clear problem statement and agent solution
- [ ] Build agent at the **repository root** (clonable root project, not a subfolder)
- [ ] Make the GitHub repository **public**
- [ ] Submit Git URL to the Project Evaluator (can resubmit multiple times)
- [ ] *(Optional)* Record a public English YouTube walkthrough (code, architecture, live demo) and include the URL

## AgentOps Code Review Matrix (Max Score: 95)

### 1. Tool & Interface Design (20 pts)
| Criteria | Code Evidence | Pts |
| :--- | :--- | :---: |
| **Comprehensive Tool Docstrings** | Tool functions include clear, human-readable descriptions of their purpose and all parameters. | 5 |
| **Descriptive Naming** | Tool names are highly specific and clear (e.g., `create_critical_bug` instead of `update_jira`). | 5 |
| **Explicit JSON Schemas** | Strict input and output schemas (e.g., Pydantic `input_schema` / `output_schema`) validate tool arguments and constrain LLMs. | 5 |
| **Guided Error Handling** | Tool error returns provide descriptive recovery instructions back to the LLM instead of crashing. | 5 |

### 2. Context & Memory (20 pts)
| Criteria | Code Evidence | Pts |
| :--- | :--- | :---: |
| **Robust System Instructions** | Clear "constitution" in system prompts defining persona, domain knowledge, and constraints. | 5 |
| **History Compaction** | Context bloat management (token truncation, sliding windows, summarization) via ADK `EventsCompactionConfig`, Vertex AI Memory Bank, or Context Caching. | 5 |
| **Persistent Session State** | Connects to a persistent store (`DatabaseSessionService`, Vector Store, or Vertex AI Search / Memory Bank) to retrieve info and manage conversational history across turns. | 5 |
| **Async Memory Operations** | Expensive memory generation and consolidation coded as `async` / background tasks to prevent blocking. | 5 |

### 3. Orchestration & Logic (20 pts)
| Criteria | Code Evidence | Pts |
| :--- | :--- | :---: |
| **Multi-Agent Patterns** | Proven ADK multi-agent patterns (`SequentialAgent`, `ParallelAgent`, `LoopAgent`, Coordinator/Sub-agents) rather than a monolith. | 5 |
| **Strategic Model Routing** | Routes requests to appropriate models (e.g., `gemini-2.5-flash` for fast tasks/triage, `gemini-2.5-pro` for deep planning/reasoning). | 5 |
| **Guardrails & Policy Plugins** | Security and evaluation guardrails (e.g., ADK `BasePlugin` / `before_model_callback` / `after_model_callback` / Model Armor / self-eval). | 5 |
| **Human-in-the-Loop Hooks** | High-stakes actions include explicit confirmation gates (e.g., ADK `require_confirmation=True` / `ToolConfirmation` / approval hooks) before execution. | 5 |

### 4. Observability & Tracing (20 pts)
| Criteria | Code Evidence | Pts |
| :--- | :--- | :---: |
| **Structured JSON Logging** | Structured JSON logging library (`python-json-logger` / `structlog` / Google Cloud Logging JSON format) capturing rich metadata instead of `print`. | 5 |
| **Intent vs. Outcome Capture** | Logs explicitly record both intended action before execution (`before_tool_callback` / `before_model_callback`) and actual outcome after (`after_tool_callback` / `after_model_callback`). | 5 |
| **Distributed Tracing** | OpenTelemetry (`TracerProvider`, nested parent-child spans, Cloud Trace exporter) linking spans end-to-end from query to answer. | 5 |
| **PII Redaction** | Active scrubbing in logging and memory pipelines redacting sensitive data before storage (regex + optional Google Cloud Sensitive Data Protection / DLP API). | 5 |

### 5. Infrastructure & CI/CD (15 pts)
| Criteria | Code Evidence | Pts |
| :--- | :--- | :---: |
| **Automated Evaluation Suites** | Testing harness against a golden dataset (`adk eval` / `AgentEvaluator` with `.test.json` / `.evalset.json` + `pytest`) to measure regressions. | 5 |
| **Infrastructure as Code** | Terraform (`deployment/terraform/*.tf`) provisioning Cloud Run, Secret Manager, IAM, etc., plus `adk` / `agent-starter-pack` CLI usage documented. | 5 |
| **Secure Secret Management** | Zero hardcoded API keys; secrets loaded via Google Cloud Secret Manager (`google-cloud-secret-manager`) with local env fallback for dev. | 5 |

## Candidate Tracks
- **Agents for Good**: Education, healthcare, or sustainability
- **Enterprise Agents**: Business workflows, data analysis, or customer support
- **Concierge Agents**: Personal productivity, meal planning, shopping, travel, etc.
- **Freestyle Track**: Innovative/unique agents outside standard categories

## Key Resources
- [ADK Documentation](https://google.github.io/adk-docs/)
- [ADK Python](https://github.com/google/adk-python) | [ADK Go](https://github.com/google/adk-go) | [ADK Java](https://github.com/google/adk-java)
- [ADK Sample Agents](https://github.com/google/adk-samples)
- [Google Cloud Agent Starter Pack](https://github.com/GoogleCloudPlatform/agent-starter-pack)
- [A2A Protocol](https://a2a-protocol.org/latest/) | [A2A Python](https://github.com/a2aproject/a2a-python)
- [Vertex AI Agent Engine Docs](https://docs.cloud.google.com/agent-builder/agent-engine/overview)
