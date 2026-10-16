import { useState } from "react";

import {
  Activity,
  Bot,
  Code2,
  GitBranch,
  Github,
  LayoutDashboard,
  Plus,
  Settings,
  ShieldCheck,
  Terminal,
} from "lucide-react";

import { api } from "./api";
import IntroExperience from "./components/IntroExperience";
import AIEngineerPage from "./pages/AIEngineerPage";
import RepositoriesPage from "./pages/RepositoriesPage";
import "./index.css";
import "./pages/pages.css";

type BackendStatus = "checking" | "connected" | "disconnected";

function App() {
  const [showWorkspace, setShowWorkspace] = useState(false);

  const [backendStatus, setBackendStatus] =
    useState<BackendStatus>("checking");

  const [activePage, setActivePage] = useState("Overview");

  const [selectedRepositoryId, setSelectedRepositoryId] = useState<
    string | null
  >(null);

  async function checkBackend() {
    setBackendStatus("checking");

    try {
      await api.health();

      setBackendStatus("connected");
    } catch {
      setBackendStatus("disconnected");
    }
  }

  function navigateTo(page: string) {
    setActivePage(page);
  }

  if (!showWorkspace) {
    return (
      <IntroExperience
        onEnter={() => setShowWorkspace(true)}
      />
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-icon">
            <Code2 size={21} />
          </div>

          <div>
            <h1>CODEMIND</h1>
            <span>ENGINEERING WORKSPACE</span>
          </div>
        </div>

        <button
          className="new-project"
          type="button"
          onClick={() => navigateTo("New project")}
        >
          <Plus size={17} />
          <span>New project</span>
        </button>

        <div className="sidebar-section">
          <p className="section-label">WORKSPACE</p>

          <button
            className={`nav-item ${
              activePage === "Overview" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Overview")}
          >
            <LayoutDashboard size={17} />
            <span>Overview</span>
          </button>

          <button
            className={`nav-item ${
              activePage === "Repositories" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Repositories")}
          >
            <GitBranch size={17} />
            <span>Repositories</span>
          </button>

          <button
            className={`nav-item ${
              activePage === "AI Engineer" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("AI Engineer")}
          >
            <Bot size={17} />
            <span>AI Engineer</span>
          </button>

          <button
            className={`nav-item ${
              activePage === "Code intelligence" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Code intelligence")}
          >
            <Activity size={17} />
            <span>Code intelligence</span>
          </button>
        </div>

        <div className="sidebar-section">
          <p className="section-label">DEVELOPMENT</p>

          <button
            className={`nav-item ${
              activePage === "Analysis jobs" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Analysis jobs")}
          >
            <Terminal size={17} />
            <span>Analysis jobs</span>
          </button>

          <button
            className={`nav-item ${
              activePage === "Security" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Security")}
          >
            <ShieldCheck size={17} />
            <span>Security</span>
          </button>
        </div>

        <div className="sidebar-bottom">
          <button
            className={`nav-item ${
              activePage === "Settings" ? "active" : ""
            }`}
            type="button"
            onClick={() => navigateTo("Settings")}
          >
            <Settings size={17} />
            <span>Settings</span>
          </button>

          <div className="user-card">
            <div className="avatar">C</div>

            <div>
              <strong>Developer</strong>
              <span>Local workspace</span>
            </div>
          </div>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Workspace</span>
            <span className="breadcrumb-divider">/</span>
            <strong>{activePage}</strong>
          </div>

          <div className="topbar-right">
            <span className="environment-badge">
              LOCAL DEVELOPMENT
            </span>

            <button
              className="icon-button"
              type="button"
              aria-label="GitHub"
              title="GitHub integration coming later"
            >
              <Github size={19} />
            </button>
          </div>
        </header>

        <section className="dashboard">
          {activePage === "Overview" ? (
            <>
              <div className="welcome-row">
                <div>
                  <p className="eyebrow">
                    YOUR ENGINEERING COMMAND CENTER
                  </p>

                  <h2>
                    Understand your code.
                    <br />
                    <span>Build with intelligence.</span>
                  </h2>

                  <p className="welcome-description">
                    Connect a repository to begin exploring architecture,
                    understanding dependencies, and analyzing your codebase.
                  </p>
                </div>

                <div className="welcome-mark">
                  <Code2 size={56} strokeWidth={1.1} />
                  <div className="orbit orbit-one" />
                  <div className="orbit orbit-two" />
                </div>
              </div>

              <div className="section-heading">
                <div>
                  <h3>Workspace overview</h3>
                  <p>Your development environment at a glance.</p>
                </div>

                <span className="local-tag">DEVELOPMENT MODE</span>
              </div>

              <div className="stats-grid">
                <article className="stat-card">
                  <div className="stat-top">
                    <span>Repositories</span>
                    <GitBranch size={18} />
                  </div>

                  <strong>0</strong>
                  <p>No repositories connected yet</p>
                </article>

                <article className="stat-card">
                  <div className="stat-top">
                    <span>Analysis jobs</span>
                    <Activity size={18} />
                  </div>

                  <strong>0</strong>
                  <p>Waiting for your first project</p>
                </article>

                <article className="stat-card">
                  <div className="stat-top">
                    <span>AI engineer</span>
                    <Bot size={18} />
                  </div>

                  <strong className="stat-text">Standby</strong>
                  <p>Repository intelligence is next</p>
                </article>
              </div>

              <div className="content-grid">
                <article className="panel repository-panel">
                  <div className="panel-heading">
                    <div>
                      <h3>Get started</h3>
                      <p>
                        Your first step into repository intelligence.
                      </p>
                    </div>

                    <div className="panel-icon">
                      <GitBranch size={19} />
                    </div>
                  </div>

                  <div className="empty-state">
                    <div className="empty-icon">
                      <Code2 size={28} />
                    </div>

                    <h4>Your workspace is ready</h4>

                    <p>
                      CODEMIND is running locally. Repository registration
                      and codebase analysis will be our next milestones.
                    </p>

                    <button
                      className="primary-button"
                      type="button"
                      onClick={checkBackend}
                      disabled={backendStatus === "checking"}
                    >
                      <Activity size={16} />

                      {backendStatus === "checking"
                        ? "Check backend connection"
                        : "Check backend connection"}
                    </button>

                    <div
                      className={`connection-status ${backendStatus}`}
                    >
                      <span className="status-dot" />

                      {backendStatus === "checking"
                        ? "Connection not checked"
                        : backendStatus === "connected"
                          ? "Backend connected successfully"
                          : "Backend connection failed"}
                    </div>
                  </div>
                </article>

                <article className="panel system-panel">
                  <div className="panel-heading">
                    <div>
                      <h3>System status</h3>
                      <p>Local development environment.</p>
                    </div>

                    <Activity size={19} />
                  </div>

                  <div className="system-row">
                    <div className="system-indicator">
                      <span className="status-dot" />
                      <span>Frontend</span>
                    </div>

                    <span className="system-value">Running</span>
                  </div>

                  <div className="system-row">
                    <div className="system-indicator">
                      <span className="status-dot" />
                      <span>API server</span>
                    </div>

                    <span className="system-value">Available</span>
                  </div>

                  <div className="system-row">
                    <div className="system-indicator">
                      <span className="status-dot" />
                      <span>Database</span>
                    </div>

                    <span className="system-value">Container ready</span>
                  </div>

                  <div className="system-row">
                    <div className="system-indicator">
                      <span className="status-dot" />
                      <span>Redis</span>
                    </div>

                    <span className="system-value">Container ready</span>
                  </div>

                  <div className="system-note">
                    <ShieldCheck size={17} />

                    <span>
                      These are initial local indicators. Live database and
                      Redis health checks will be implemented separately.
                    </span>
                  </div>
                </article>
              </div>
            </>
          ) : activePage === "Repositories" ? (
            <RepositoriesPage
              onAsk={(repositoryId) => {
                setSelectedRepositoryId(repositoryId);
                navigateTo("AI Engineer");
              }}
            />
          ) : activePage === "AI Engineer" ? (
            <AIEngineerPage
              repositoryId={selectedRepositoryId}
              onSelectRepository={setSelectedRepositoryId}
            />
          ) : (
            <div className="welcome-row">
              <div>
                <p className="eyebrow">CODEMIND WORKSPACE</p>

                <h2>
                  {activePage}
                  <br />
                  <span>Coming into focus.</span>
                </h2>

                <p className="welcome-description">
                  This section is ready for its next implementation phase.
                  The repository intelligence, analysis workflows, and
                  engineering tools will be added here.
                </p>

                <button
                  className="primary-button"
                  type="button"
                  onClick={() => navigateTo("Overview")}
                >
                  <LayoutDashboard size={16} />
                  Back to overview
                </button>
              </div>
            </div>
          )}

          <footer className="dashboard-footer">
            <span>CODEMIND · AI ENGINEERING WORKSPACE</span>
            <span>PHASE 1 / FOUNDATION</span>
          </footer>
        </section>
      </main>
    </div>
  );
}

export default App;