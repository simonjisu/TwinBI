# Streamlit + Cube–Based NLQ Dashboard / Chat + Interactive Plots  
## Implementation Documentation Outline

## 0. Objective
Build an application where Streamlit is used as the frontend. Users can enter **natural language queries (NLQ)** in a chat UI, and the system will:

1) Convert the NLQ into a **Cube Query (JSON)** and fetch the required data from Cube  
2) Render **interactive plots (e.g., Plotly)** based on the results  
3) Detect and persist **user interactions (clicks/selections)** on charts in the backend  
4) Reuse the selected context in subsequent NLQ, visualizations, and drill-down flows  

---

## 1. User Actions

### A. Chat / Query Actions
- **A1. Enter a natural language question**
  - Example: “Show me sales trends for the last 30 days”, “Top 10 products by sales in APAC”
- **A2. Modify or follow up on a question**
  - Example: “Change that to weekly”, “Exclude refunds”
- **A3. Re-run / refresh results**
  - Re-execute the same query, optionally bypassing cache
- **A4. Reset conversation context**
  - Clear filters, selections, and chat history

### B. Visualization Actions
- **B1. Change chart type**
  - Line ↔ Bar ↔ Area ↔ Table, etc.
- **B2. Change measures / dimensions**
  - Measure switch (Revenue → Orders), dimension switch (Month → Week)
- **B3. Adjust range / sorting / Top-N**
  - Change date range, sort order, Top 10 / Top 20
- **B4. Download / export (optional)**
  - CSV export, image export

### C. Interactive / Drill Actions
- **C1. Click a chart element**
  - Select a specific bar/point to choose a dimension value (e.g., region = APAC)
- **C2. Drag selection (brushing)**
  - Select a time window (e.g., 12/01–12/15)
- **C3. Clear selection**
  - Remove a specific filter or reset all filters
- **C4. Drill-down / drill-through**
  - Drill from region → country → city
  - View underlying rows or detail tables for a selected point (with performance and permission controls)

### D. Session / State Actions
- **D1. View current selection and filters**
  - UI panel showing applied filters (e.g., “Region: APAC”)
- **D2. Share / restore state (optional)**
  - Generate and restore permalinks (query + filters + chart state)

---

## 2. Functionalities Required to Support These Actions

### 2.1 NLQ Processing Pipeline
- **NLQ Parsing / Intent Detection**
  - Convert user input into structured intent (metrics, dimensions, filters, time range)
- **Query Planning**
  - Decide which Cube members (measures/dimensions/segments) to use
  - Split into multiple queries if needed (KPI cards + trend + ranking)
- **Cube Query Generation**
  - Create Cube `/load` JSON queries
  - Includes `measures`, `dimensions`, `timeDimensions`, `filters`, `order`, `limit`
- **(Optional) LLM-based NLQ → Query**
  - Use an LLM referencing Cube schema
  - Guardrails: allow-list, validation, rule-based fallback

### 2.2 Cube Integration Layer
- **Cube Client**
  - Authentication (JWT/API token), request abstraction, error handling, retries
- **Query Validation**
  - Prevent invalid members, validate types and operators
- **Caching Strategy**
  - Client-side caching for repeated NLQ/filter combinations
  - Cube-side caching with pre-aggregations
- **Observability**
  - Logging and metrics for latency, cache hits, and errors

### 2.3 Visualization Generation Layer
- **Chart Spec Builder**
  - Convert dataframes into Plotly figures
  - Tooltip, label, and axis formatting
- **Chart Registry**
  - Manage `figure_id`, linked query specs, and interaction mappings
- **Data Contracts**
  - Define how chart interactions map to Cube filters
  - Inject `cube_member` and `value` into Plotly `customdata`

### 2.4 Interactivity (Click / Brush) Handling
- **Event Capture**
  - Receive Plotly click/selection events (e.g., via `streamlit-plotly-events`)
- **Event → Filter Translator**
  - Convert event payloads into Cube filter objects
  - Example: clicking a bar adds a `region = APAC` filter
- **State Update**
  - Persist `active_filters`, `last_selection`, and `selected_figure` in session state
- **Re-render Trigger**
  - Re-query Cube and re-render affected charts on state changes

### 2.5 Chat UI / Context Management
- **Conversation Store**
  - Persist message history and system context (selected filters)
- **Context Injection**
  - Inject current `active_filters` into NLQ processing and prompts
- **Answer Composer**
  - Combine text explanations, charts, and KPI summaries
  - Clearly display applied filters and selections in the UI

### 2.6 Security & Governance
- **Authentication / Authorization**
  - User-level access control (tenant/role-based)
  - Combined Cube permissions and application-level constraints
- **Query Guardrails**
  - Allow-listed members, row limits, time range limits
  - Mask sensitive data
- **Audit Logging**
  - Log NLQs, generated Cube queries, click events, and filter changes

---

## 3. Concrete Architecture

### 3.1 High-Level Components
- **Streamlit App (Frontend)**
  - Chat UI, dashboard layout, chart rendering, interaction capture
- **Application Backend (Python Layer)**
  - NLQ processing, query planning, Cube client, state/cache management
  - Can run inside the Streamlit process initially
- **Cube (Semantic Layer + Query Engine)**
  - Defines measures/dimensions/segments, caching, pre-aggregations, data sources
- **Data Warehouse / Database**
  - BigQuery, Snowflake, Postgres, etc.
- **(Optional) Redis**
  - Server-side shared state for selections and context
  - Recommended for multi-worker or scaled deployments
- **(Optional) LLM Service**
  - NLQ → Cube query generation and result summarization

---

### 3.2 Runtime Flows (Key Scenarios)

#### Flow 1: NLQ → Plot Rendering
1. User submits a natural language query
2. Backend parses NLQ with current `active_filters`
3. Cube query (JSON) is generated and sent to `/load`
4. Cube returns results (cache / pre-aggregations applied)
5. Backend converts results to a dataframe
6. Chart Spec Builder generates Plotly figures
7. Streamlit renders charts and textual responses

#### Flow 2: Chart Click → Filter Update → Re-query
1. User clicks or selects a chart element
2. Streamlit captures the interaction event
3. Backend translates the event into Cube filters
4. `active_filters` are updated in session state (or Redis)
5. Affected charts trigger new Cube `/load` requests
6. Charts and text are re-rendered with updated results
7. Chat context reflects the current selection

#### Flow 3: Follow-up Query with Context
1. User enters a follow-up like “Redo this for APAC only”
2. Backend injects `active_filters` and `last_selection` into context
3. Cube queries are generated and executed
4. Results and visualizations are updated accordingly

---

### 3.3 State Design

#### Session State (Default)
- `conversation`: list of `{role, content, ts}`
- `active_filters`: list of Cube filters
- `last_selection`: `{figure_id, payload, derived_filters, ts}`
- `last_queries`: `{figure_id: cube_query_json}`
- `ui_preferences`: `{chart_type, theme, ...}`

#### Server-side State (Extended)
- Redis key examples:
  - `ctx:{client_id}` → active_filters + last_selection + conversation_summary
  - `cache:nlq:{hash}` → NLQ → Cube query mapping (optional)

---

## 3.4 Simplified Folder & Module Structure (UI + Backend)

This project is split into two top-level parts:

- `ui/` (Streamlit): chat UI, dashboard UI, and interaction event handling (click/brush).
- `backend/` (FastAPI): NLQ processing, Cube querying, state store, and chart spec generation.

---

### 3.4.1 Folder Layout

The project is organized into two main folders: `ui` and `backend`.

- `ui` contains all Streamlit-related code for rendering the user interface and handling user interactions.
- `backend` contains a FastAPI application that handles NLQ processing, Cube queries, visualization generation, and state management.

```
repo/
  ui/
    app.py                     # Streamlit entry point
    pages/
      dashboard.py             # Main dashboard page
    components/
      chat_panel.py            # Chat UI components
      chart_panel.py           # Chart rendering components
      filters_panel.py         # UI for showing and editing active filters
    events/
      chart_events.py          # Chart click/selection event handlers
    state/
      session_state.py         # Streamlit session_state helpers

  backend/
    main.py                    # FastAPI entry point
    api/
      routes.py                # API routes (NLQ, query, visualization, state)
      schemas.py               # Pydantic request/response models
    services/
      nlq_service.py           # Natural language query processing
      cube_service.py          # Cube /load (or SQL API) integration
      viz_service.py           # Build visualization specs (e.g., Plotly JSON)
      state_service.py         # Context/state store abstraction
    core/
      config.py                # Environment and app configuration
      utils.py                 # Shared utility functions
```

---

### 3.4.2 Responsibilities by Layer

#### UI (Streamlit) responsibilities
- Render chat messages and dashboard layout
- Display plots/tables returned from backend
- Capture user interactions on charts (click/select/brush)
- Maintain lightweight per-session UI state:
  - `active_filters` (what the user selected)
  - `last_selection` (last clicked figure + payload)
  - `conversation` (chat history)
- Send user requests and interaction events to FastAPI

#### Backend (FastAPI) responsibilities
- Convert NLQ into a Cube query (JSON) and/or a query plan
- Execute Cube queries and return results in a UI-friendly format
- Build chart specifications from query results (e.g., Plotly JSON spec)
- Store/retrieve user context (filters, last selection) via state service

---

### 3.4.3 API Endpoints (Suggested)

#### 1) NLQ -> Plan
- `POST /nlq/plan`
  - Input: `{ session_id, message, active_filters, last_selection }`
  - Output: `{ plan, suggested_visuals }`
  - Purpose: parse NLQ and decide which Cube query(ies) to run and which visuals to render

#### 2) Execute Cube Query
- `POST /cube/load`
  - Input: `{ session_id, cube_query_json }`
  - Output: `{ data, meta }`
  - Purpose: execute Cube `/load` and return tabular results

#### 3) Build Visualization Spec
- `POST /viz/build`
  - Input: `{ session_id, figure_id, data, chart_type, mapping }`
  - Output: `{ figure_id, plotly_spec, interaction_mapping }`
  - Purpose: backend produces a Plotly spec and also returns how interactions map to filters

> Option: you can combine (2) and (3) into one endpoint:
- `POST /query/render`
  - Input: `{ session_id, message_or_plan, active_filters }`
  - Output: `{ cards, figures, tables, updated_context }`

#### 4) State (optional but recommended)
- `GET /state/context?session_id=...`
- `POST /state/context`
  - Purpose: store and retrieve user context from a server-side store (redis/in-memory)

---

### 3.4.4 Interaction Handling Flow (UI-driven)

1. UI renders a Plotly figure received from backend
2. User clicks/selects/brushes on the plot
3. UI event handler extracts payload and maps it to a semantic selection:
   - `figure_id`, `selected_dimension_value`, `time_range`, etc.
4. UI updates Streamlit `session_state` (fast feedback)
5. UI sends the event to backend:
   - `POST /state/context` (persist)
   - `POST /query/render` (re-render dependent charts)
6. UI re-renders updated figures and chat response

---

### 3.4.5 Notes on Where “viz” Lives

You can place `viz` entirely in the backend, as requested:
- Backend returns either:
  - Plotly JSON spec (recommended for consistent rendering)
  - or raw data + chart instruction (UI builds plots)

Recommended: backend returns Plotly JSON spec and interaction mapping.
This keeps UI thin and makes behavior consistent across pages.

---

### 3.4.6 Minimal “Contract” Between UI and Backend

UI sends:
- `session_id`
- `conversation` or latest `message`
- `active_filters`
- `last_selection` (figure_id + payload)
- (optional) `ui_preferences` (chart type, topN)

Backend returns:
- `assistant_message` (text)
- `updated_context` (filters/selection)
- `figures`: list of `{ figure_id, plotly_spec, interaction_mapping }`
- `tables`: list of tabular results (optional)
---

## 4. Non-Functional Requirements Checklist
- Performance: p95 latency targets, cache hit ratios
- Correctness: NLQ → Query validation and test cases
- Reliability: timeouts, retries, circuit breakers
- Security: authorization, masking, audit logs
- Scalability: Redis requirement for multi-worker deployments

---

## 5. Suggested Implementation Roadmap
1) **MVP**
   - Fixed Cube query templates + Plotly rendering + click events stored in session state
2) **Introduce NLQ**
   - Rule-based NLQ for time ranges, Top-N, basic filters
3) **Dashboard Interlinking**
   - Click-based filters propagated across multiple charts
4) **Production Readiness**
   - Redis-backed state, pre-aggregations, observability, authorization
5) **Advanced Features**
   - LLM-based NLQ, drill-through, permalinks, insight recommendations


# How to Start

1. Start backend (`uvicorn backend.main:app --reload --port 8000`) and UI (`streamlit run ui/app.py`), setting BACKEND_URL if the API isn’t on localhost.
2. Point `CUBE_API_URL`/`CUBE_API_TOKEN` to a running Cube instance to replace the mock data path.
3. Try a query like “weekly revenue by state last 30 days,” click a chart bar to see filters propagate, and reset via the filters panel when needed.