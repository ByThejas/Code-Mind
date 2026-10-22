import { useEffect, useState } from "react";

import {
  ArrowRight,
  Bot,
  GitBranch,
  LayoutDashboard,
  Network,
  Settings,
  ShieldCheck,
  Terminal,
  type LucideIcon,
} from "lucide-react";

import { api, isServable, type Repository } from "./api";
import ChromeSphere from "./components/ChromeSphere";
import AIEngineerPage from "./pages/AIEngineerPage";
import ArchitecturePage from "./pages/ArchitecturePage";
import RepositoriesPage from "./pages/RepositoriesPage";
import "./index.css";
import "./pages/pages.css";

type BackendStatus = "checking" | "connected" | "disconnected";

interface NavEntry {
  page: string;
  icon: LucideIcon;
}

const MAIN_NAV: NavEntry[] = [
  { page: "Overview", icon: LayoutDashboard },
  { page: "Repositories", icon: GitBranch },
  { page: "AI Engineer", icon: Bot },
  { page: "Architecture", icon: Network },
];

const LATER_NAV: NavEntry[] = [
  { page: "Analysis jobs", icon: Terminal },
  { page: "Security", icon: ShieldCheck },
  { page: "Settings", icon: Settings },
];

function App() {
  const [backendStatus, setBackendStatus] =
    useState<BackendStatus>("checking");

  const [repositories, setRepositories] = useState<Repository[] | null>(
    null,
  );

  const [activePage, setActivePage] = useState("Overview");

  const [selectedRepositoryId, setSelectedRepositoryId] = useState<
    string | null
  >(null);

  useEffect(() => {
    let cancelled = false;

    api
      .health()
      .then(() => !cancelled && setBackendStatus("connected"))
      .catch(() => !cancelled && setBackendStatus("disconnected"));

    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (activePage !== "Overview") return;

    let cancelled = false;

    api
      .listRepositories()
      .then((result) => !cancelled && setRepositories(result.items))
      .catch(() => !cancelled && setRepositories(null));

    return () => {
      cancelled = true;
    };
  }, [activePage]);

  const indexed = repositories?.filter(isServable) ?? [];
  const filesIndexed = indexed.reduce(
    (total, repository) =>
      total + (repository.latest_snapshot?.stats.files_included ?? 0),
    0,
  );

  function renderNav(entries: NavEntry[]) {
    return entries.map(({ page, icon: Icon }) => (
      <button
        key={page}
        className={`nav-item ${activePage === page ? "active" : ""}`}
        type="button"
        onClick={() => setActivePage(page)}
      >
        <Icon size={16} strokeWidth={1.6} />
        <span>{page}</span>
      </button>
    ));
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark" />
          <h1>CodeMind</h1>
        </div>

        <nav className="sidebar-section">{renderNav(MAIN_NAV)}</nav>

        <nav className="sidebar-section later">
          <p className="section-label">Coming later</p>
          {renderNav(LATER_NAV)}
        </nav>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <strong>{activePage}</strong>

          <span className={`api-status ${backendStatus}`}>
            <span className="status-dot" />
            {backendStatus === "checking"
              ? "Checking API"
              : backendStatus === "connected"
                ? "API online"
                : "API offline"}
          </span>
        </header>

        <section className="dashboard">
          {activePage === "Overview" ? (
            <>
              <div className="hero">
                <div className="hero-copy">
                  <p className="eyebrow">Local · Free models</p>

                  <h2>
                    Understand your code,
                    <br />
                    <em>with evidence.</em>
                  </h2>

                  <p className="hero-description">
                    Index a GitHub repository, then ask questions and get
                    answers that cite the exact files and lines.
                  </p>

                  <div className="hero-actions">
                    <button
                      className="primary-button"
                      type="button"
                      onClick={() => setActivePage("Repositories")}
                    >
                      Add a repository
                      <ArrowRight size={15} />
                    </button>

                    <button
                      className="glass-button"
                      type="button"
                      onClick={() => setActivePage("AI Engineer")}
                    >
                      Ask the AI Engineer
                    </button>
                  </div>
                </div>

                <ChromeSphere />
              </div>

              <div className="stat-strip">
                <div className="stat">
                  <span>Repositories</span>
                  <strong>{repositories ? repositories.length : "–"}</strong>
                </div>

                <div className="stat">
                  <span>Ready to ask</span>
                  <strong>{repositories ? indexed.length : "–"}</strong>
                </div>

                <div className="stat">
                  <span>Files indexed</span>
                  <strong>
                    {repositories ? filesIndexed.toLocaleString() : "–"}
                  </strong>
                </div>
              </div>
            </>
          ) : activePage === "Repositories" ? (
            <RepositoriesPage
              onAsk={(repositoryId) => {
                setSelectedRepositoryId(repositoryId);
                setActivePage("AI Engineer");
              }}
            />
          ) : activePage === "AI Engineer" ? (
            <AIEngineerPage
              repositoryId={selectedRepositoryId}
              onSelectRepository={setSelectedRepositoryId}
            />
          ) : activePage === "Architecture" ? (
            <ArchitecturePage
              repositoryId={selectedRepositoryId}
              onSelectRepository={setSelectedRepositoryId}
            />
          ) : (
            <div className="placeholder">
              <p className="eyebrow">Coming later</p>
              <h2>{activePage}</h2>
              <p className="hero-description">
                This section arrives in a later phase of the roadmap.
              </p>
              <button
                className="glass-button"
                type="button"
                onClick={() => setActivePage("Overview")}
              >
                Back to overview
              </button>
            </div>
          )}
        </section>
      </main>
    </div>
  );
}

export default App;
