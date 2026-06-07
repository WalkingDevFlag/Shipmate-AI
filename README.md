# 🚀 ShipMate AI — Agentic Engineering Command Center

> **Microsoft-First SaaS Product**: AI-Powered Production Function — Reinventing Work

[![CI](https://github.com/WalkingDevFlag/Shipmate-AI/actions/workflows/ci.yml/badge.svg?branch=feat%2Fbedrock-on-v2)](https://github.com/WalkingDevFlag/Shipmate-AI/actions/workflows/ci.yml?query=branch%3Afeat%2Fbedrock-on-v2)

---

## ✨ What It Does

**ShipMate AI** connects to your GitHub repository, accepts a feature request, and generates a **delivery-readiness analysis** using 5 specialized AI agents running as an agentic swarm.

Paste a feature request → Click **Analyze Delivery Readiness** → Watch 5 agents execute in sequence:

| Agent | Role | Output |
|-------|------|--------|
| 🧠 **Planner Agent** | Decomposes feature into structured engineering tasks | Frontend/backend/DB tasks, story points, sprint count, deployment risks |
| 🔍 **Repo Analyst Agent** | Maps feature impact across the codebase | Files to change, affected APIs, risky dependencies, architecture notes |
| 🧪 **Test Architect Agent** | Creates a complete test suite for the feature | Unit tests, API tests, edge cases, regression checklist, coverage estimate |
| 🛡️ **Security Guard Agent** | Identifies security vulnerabilities and compliance risks | CVEs, exposed secrets, auth bypass risks, OWASP findings, dependency warnings |
| 🚢 **Delivery Manager Agent** | Produces a release-ready sprint plan | Sprint plan, standup summary, PR review notes, CI/CD recommendations |

**Final Output**: Production Readiness Score (0–100) with detailed breakdown + full Markdown report export.

---

## 🏛️ Microsoft-First Architecture

### Frontend
- **React + TypeScript** with Vite
- **Tailwind CSS** for premium SaaS styling
- **Framer Motion** for smooth animations
- **Deployed to**: Azure Static Web Apps
- **GitHub-First UI** with repo/branch selection and feature request input

### Backend
- **FastAPI** with Python 3.11+
- **Read-only GitHub API** integration
- **Mock agent outputs** for demo (Azure OpenAI integration ready)
- **Deployed to**: Azure App Service
- **Environment-ready** for Azure Key Vault, Application Insights

### AI & Cloud Services
- **AI Model**: Azure OpenAI (GPT-4) via Azure AI Foundry
- **Database**: Azure Cosmos DB (report metadata)
- **Storage**: Azure Blob Storage (exported reports)
- **Secrets**: Azure Key Vault
- **Telemetry**: Application Insights
- **CI/CD**: GitHub Actions
- **Authentication**: GitHub App (OAuth)

---

## 🚀 Quick Start with GitHub OAuth

### Prerequisites
- GitHub account
- Node.js and Python installed
- GitHub OAuth App credentials (see setup guide)

### Setup Steps

1. **Create GitHub OAuth App**:
   - Visit https://github.com/settings/developers/oauth-apps
   - Click "New OAuth App"
   - Set Authorization callback URL to `http://localhost:8000/github-callback.html`
   - Copy Client ID and Client Secret

2. **Configure Environment**:
   ```bash
   # Backend
   cd backend
   cp .env.example .env
   # Edit .env with your GitHub credentials:
   # GITHUB_CLIENT_ID=your_client_id
   # GITHUB_CLIENT_SECRET=your_client_secret
   ```

3. **Install Dependencies**:
   ```bash
   # Backend
   cd backend
   pip install -r requirements.txt
   
   # Frontend
   cd frontend
   npm install
   ```

4. **Start the Application**:
   ```bash
   # Terminal 1: Backend
   cd backend
   python -m uvicorn app.main:app --reload
   
   # Terminal 2: Frontend
   cd frontend
   npm run dev
   ```

5. **Open Dashboard**:
   - Visit http://localhost:5174
   - Click "Connect Your GitHub Account"
   - Select a repository and run analysis

**📖 Detailed Setup**: See [GITHUB_OAUTH_SETUP.md](GITHUB_OAUTH_SETUP.md) for complete instructions, troubleshooting, and production deployment.

---

### User Flow
1. User opens ShipMate AI dashboard
2. User clicks "Connect GitHub"
3. User authorizes ShipMate GitHub App (read-only)
4. User selects a repository
5. User selects a branch
6. User enters a feature request or issue description
7. User clicks "Analyze Delivery Readiness"
8. Four agents run and generate analysis
9. Dashboard displays results + export option

### GitHub App Permissions (Minimum)
- **Contents**: read-only
- **Metadata**: read-only
- **Pull requests**: read-only
- **Issues**: read-only
- **Actions**: read-only

### Security Principles
- ✅ Never expose GitHub tokens in frontend
- ✅ Never expose Azure OpenAI keys in frontend
- ✅ Backend handles all sensitive API calls
- ✅ Mock agent outputs if credentials unavailable
- ✅ Trust message: "ShipMate uses read-only GitHub access. We analyze selected repositories only and never modify your code."

---

## 🗂️ Project Structure

```
shipmate-ai/
├── frontend/                    # React + TypeScript SaaS dashboard
│   ├── src/
│   │   ├── App.tsx            # GitHub-first main app
│   │   ├── components/        # Reusable UI components
│   │   │   ├── Sidebar.tsx    # Navigation sidebar
│   │   │   ├── AgentSwarm.tsx # Agent status timeline
│   │   │   ├── ReadinessScore.tsx # Score visualization
│   │   │   ├── ReportTabs.tsx # Analysis result tabs
│   │   │   └── tabs/          # Individual tab components
│   │   ├── lib/
│   │   │   └── api.ts         # API client
│   │   ├── types/
│   │   │   └── index.ts       # TypeScript interfaces
│   │   ├── App.css
│   │   └── index.css
│   ├── package.json
│   ├── tsconfig.json
│   └── vite.config.ts
│
├── backend/                     # FastAPI Python backend
│   ├── app/
│   │   ├── main.py            # FastAPI app + endpoints
│   │   ├── agents/            # AI agents
│   │   │   ├── planner.py
│   │   │   ├── repo_analyst.py
│   │   │   ├── test_generator.py
│   │   │   ├── security_guard.py
│   │   │   └── delivery_manager.py
│   │   ├── services/          # Business logic
│   │   │   ├── analyzer.py    # Main analysis engine
│   │   │   ├── github_service.py # GitHub integration
│   │   │   ├── repo_service.py   # Repository utilities
│   │   │   └── azure_openai_service.py # LLM abstraction
│   │   └── models/
│   │       └── schemas.py     # Pydantic models
│   ├── requirements.txt
│   ├── verify.py
│   └── main.py (for local dev)
│
└── README.md                    # This file
```

---

## 🔌 API Reference

### GitHub Integration Endpoints

#### `GET /api/github/app-install-url`
Get GitHub App installation URL for OAuth flow.

```
Response:
{
  "install_url": "https://github.com/apps/shipmate-ai/installations/new",
  "permissions": ["Contents: read-only", ...],
  "message": "Click to authorize..."
}
```

#### `GET /api/github/repos`
Get list of repositories accessible to user.

```
Response: [
  {
    "id": "repo-1",
    "name": "shipmate-frontend",
    "full_name": "myorg/shipmate-frontend",
    "description": "...",
    "tech_stack": ["React", "TypeScript"],
    "stars": 42,
    "language": "TypeScript"
  },
  ...
]
```

#### `GET /api/github/repos/{repoId}/branches`
Get branches for a repository.

```
Response: [
  {
    "name": "main",
    "commit": {
      "sha": "abc123...",
      "message": "feat: ..."
    }
  },
  ...
]
```

### Analysis Endpoints

#### `POST /api/analyze`
Run full 5-agent production analysis.

```
Request:
{
  "repo_id": "repo-1",
  "branch": "main",
  "feature_request": "Add GitHub OAuth login...",
  "github_token": "gho_..."  // optional
}

Response:
{
  "repo_summary": {
    "name": "shipmate-frontend",
    "branch": "main",
    "tech_stack": ["React", "TypeScript"],
    "files_to_modify": ["src/App.tsx", ...],
    "stars": 42,
    "language": "TypeScript"
  },
  "agents": {
    "planner": { ... },
    "repo_analyst": { ... },
    "test_generator": { ... },
    "security_guard": { ... },
    "delivery_manager": { ... }
  },
  "readiness_score": 82,
  "recommendation": "Needs fixes before shipping",
  "summary": "ShipMate AI analyzed your feature request...",
  "markdown_report": "# Production Readiness Report\n...",
  "score_breakdown": {
    "task_clarity": 28,
    "test_coverage": 75,
    "security_score": 72,
    ...
  }
}
```

### Report Export

#### `POST /api/reports/export`
Generate exportable report.

#### `GET /api/reports/{analysisId}/download`
Download report as file.

---

## 🚀 Quick Start

### Prerequisites
- Node.js 18+ (frontend)
- Python 3.11+ (backend)
- Git

### Local Development

#### 1. Clone Repository
```bash
git clone https://github.com/Namanns7cr7/Shipmate-AI.git
cd Shipmate-AI
```

#### 2. Backend Setup
```bash
cd backend

# Create virtual environment
python -m venv venv
source venv/bin/activate
  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Set environment variables (optional for demo)
export AZURE_OPENAI_ENDPOINT="https://your-resource.openai.azure.com/"
export AZURE_OPENAI_API_KEY="your-key"
export AZURE_OPENAI_DEPLOYMENT_NAME="gpt-4"

# Run backend
python main.py
# Server runs on http://localhost:8000
```

#### 3. Frontend Setup
```bash
cd frontend

# Install dependencies
npm install

# Start dev server
npm run dev
# App runs on http://localhost:5173
```

#### 4. Open Dashboard
Visit `http://localhost:5173` in your browser.

Click "Connect GitHub" to start analyzing repositories!

---

## 🔐 Environment Variables

### Backend (.env)
```
# Azure OpenAI
AZURE_OPENAI_ENDPOINT=https://your-resource.openai.azure.com/
AZURE_OPENAI_API_KEY=your-api-key
AZURE_OPENAI_DEPLOYMENT_NAME=gpt-4

# GitHub App
GITHUB_APP_ID=123456
GITHUB_APP_PRIVATE_KEY="-----BEGIN RSA PRIVATE KEY-----\n..."
GITHUB_WEBHOOK_SECRET=your-secret

# Azure Services
AZURE_STORAGE_CONNECTION_STRING=DefaultEndpointsProtocol=https;...
AZURE_COSMOS_CONNECTION_STRING=AccountEndpoint=https://...;
APPLICATIONINSIGHTS_CONNECTION_STRING=InstrumentationKey=...
```

---

## 🧪 Testing Agents Locally

### Run Verification
```bash
cd backend
python verify.py
```

This runs all agents with sample data and validates outputs.

---

## 🔄 Agent Details

### Planner Agent
**Input**: Feature request + repo context
**Output**: 
- Frontend tasks
- Backend tasks
- Database changes
- Test requirements
- Deployment risks
- Story points estimate
- Sprint count

### Repo Analyst Agent
**Input**: Feature request + repo context
**Output**:
- Files to change (with risk levels)
- Affected APIs
- Risky dependencies
- Patterns to follow
- Impact score
- Architecture notes

### Test Architect Agent
**Input**: Feature request + repo context
**Output**:
- Unit tests
- API integration tests
- Edge case scenarios
- Regression checklist
- Test coverage estimate

### Security Guard Agent
**Input**: Feature request + repo context
**Output**:
- Security risks (critical/high/medium/low)
- Exposed secrets check
- Auth bypass risks
- Dependency vulnerabilities
- Unsafe tool calls
- Overall security score

### Delivery Manager Agent
**Input**: All agent outputs + feature request
**Output**:
- Sprint plan (day-by-day tasks)
- Standup summary
- PR review summary
- CI/CD recommendations
- Release readiness score
- Next actions
- Estimated release date

---

## 🚢 Deployment

### Deploy to Azure

#### Frontend: Azure Static Web Apps
```bash
# Build
cd frontend
npm run build

# Deploy via Azure CLI or GitHub Actions
az staticwebapp create --name shipmate-ai --source . --location eastus
```

#### Backend: Azure App Service
```bash
# Create App Service
az appservice plan create --name shipmate-plan --sku B1 --is-linux
az webapp create --plan shipmate-plan --name shipmate-api --runtime "python|3.11"

# Deploy
az webapp up --name shipmate-api --runtime "python:3.11"
```

---

## 📊 Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    Azure Static Web Apps                         │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │     React + TypeScript Frontend (GitHub-First UI)       │  │
│  │  - GitHub repo selection                                │  │
│  │  - Branch selector                                      │  │
│  │  - Feature request input                                │  │
│  │  - Live agent timeline                                  │  │
│  │  - Analysis result dashboard                            │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                               ↕ HTTPS
┌─────────────────────────────────────────────────────────────────┐
│                    Azure App Service                             │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │      FastAPI Backend + Agent Swarm                       │  │
│  │                                                          │  │
│  │  ┌────────────────────────────────────────────────┐    │  │
│  │  │ GitHub Service Layer                           │    │  │
│  │  │ - OAuth flow                                   │    │  │
│  │  │ - Repo/branch enumeration                      │    │  │
│  │  └────────────────────────────────────────────────┘    │  │
│  │                    ↕                                    │  │
│  │  ┌────────────────────────────────────────────────┐    │  │
│  │  │ Agentic Swarm Engine                           │    │  │
│  │  │ ① Planner Agent       ③ Test Architect        │    │  │
│  │  │ ② Repo Analyst        ④ Security Guard        │    │  │
│  │  │ ⑤ Delivery Manager                            │    │  │
│  │  └────────────────────────────────────────────────┘    │  │
│  │                    ↕                                    │  │
│  │  ┌────────────────────────────────────────────────┐    │  │
│  │  │ LLM Abstraction Layer                          │    │  │
│  │  │ - Mock outputs (demo)                          │    │  │
│  │  │ - Azure OpenAI (production)                    │    │  │
│  │  └────────────────────────────────────────────────┘    │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                        ↕
        ┌───────────────────────────────┐
        │    Azure Services Stack       │
        ├───────────────────────────────┤
        │ • Azure OpenAI (GPT-4)       │
        │ • Azure Cosmos DB             │
        │ • Azure Blob Storage          │
        │ • Azure Key Vault             │
        │ • Application Insights        │
        └───────────────────────────────┘
                        ↕
        ┌───────────────────────────────┐
        │    GitHub API                 │
        │ (Read-only access)            │
        └───────────────────────────────┘
```

---

## 🛠️ Development Roadmap

### Phase 1: MVP (Current) ✅
- [x] GitHub OAuth integration (mock)
- [x] Repo/branch selection UI
- [x] Feature request input
- [x] 5-agent swarm architecture
- [x] Mock agent outputs
- [x] Readiness score calculation
- [x] Report export (Markdown)

### Phase 2: Real Azure Integration
- [ ] Azure OpenAI API integration
- [ ] Real GitHub App OAuth flow
- [ ] Azure Cosmos DB storage
- [ ] Azure Blob Storage for reports
- [ ] Application Insights telemetry

### Phase 3: Enterprise Features
- [ ] Multi-team workspaces
- [ ] Report history & comparison
- [ ] Webhook integrations
- [ ] API rate limiting
- [ ] Advanced analytics

### Phase 4: Scale & Compliance
- [ ] SOC 2 compliance
- [ ] HIPAA/GDPR support
- [ ] Multi-region deployment
- [ ] Advanced audit logging

---

## 🤝 Contributing

### Code Style
- **Frontend**: Prettier + ESLint
- **Backend**: Black + isort

### Branch Strategy
- `main` → Production
- `develop` → Staging
- `feature/*` → Feature branches

### Pull Request Process
1. Fork the repo
2. Create feature branch (`feature/my-feature`)
3. Commit changes
4. Push to branch
5. Open Pull Request
6. Address review feedback
7. Merge when approved

---

## 📄 License

MIT License — See LICENSE file for details.

---

## 🙏 Acknowledgments

Built with ❤️ using:
- **React** + **TypeScript** + **Tailwind CSS** + **Framer Motion**
- **FastAPI** + **Pydantic** + **Python**
- **Azure OpenAI** + **Azure Services**
- **GitHub API**

---

## 📞 Support

- 📧 Email: support@shipmate.ai
- 🐛 Issues: GitHub Issues
- 💬 Discussions: GitHub Discussions
- 📖 Docs: [Full Documentation](https://docs.shipmate.ai)

---

**ShipMate AI** — Where Code Meets Intelligence ⚡

*Powered by Azure AI Foundry + Azure OpenAI*
