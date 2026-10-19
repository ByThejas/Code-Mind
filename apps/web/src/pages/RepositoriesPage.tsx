import { type FormEvent, useCallback, useEffect, useRef, useState } from "react";

import {
  AlertTriangle,
  Bot,
  GitBranch,
  GitCommit,
  Loader2,
  Plus,
  RefreshCw,
  Square,
} from "lucide-react";

import {
  ACTIVE_SNAPSHOT_STATUSES,
  ApiError,
  type Job,
  type Repository,
  api,
  isServable,
  repositoryName,
} from "../api";

const POLL_MS = 2000;
const JOBS_STORAGE_KEY = "codemind.trackedJobs";

const STAGE_LABELS: Record<string, string> = {
  resolve: "Resolving commit",
  fetch: "Downloading archive",
  filter: "Filtering files",
  parse: "Parsing files",
  store: "Storing files and chunks",
  graph: "Linking calls and imports",
  embed: "Embedding chunks",
  finalize: "Finishing up",
  retry: "Waiting to retry",
};

function loadTrackedJobs(): Record<string, string> {
  try {
    return JSON.parse(localStorage.getItem(JOBS_STORAGE_KEY) ?? "{}");
  } catch {
    return {};
  }
}

function saveTrackedJobs(jobs: Record<string, string>) {
  try {
    localStorage.setItem(JOBS_STORAGE_KEY, JSON.stringify(jobs));
  } catch {
    // Storage is a convenience; polling the repository list still shows status.
  }
}

function formatDate(value: string | null): string {
  if (!value) return "—";
  return new Date(value).toLocaleString(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  });
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong.";
}

interface RepositoriesPageProps {
  onAsk: (repositoryId: string) => void;
}

export default function RepositoriesPage({ onAsk }: RepositoriesPageProps) {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [listError, setListError] = useState<string | null>(null);

  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Repository id → id of the index job this browser started for it.
  const [trackedJobs, setTrackedJobs] = useState<Record<string, string>>(loadTrackedJobs);
  const [jobs, setJobs] = useState<Record<string, Job>>({});
  const [notices, setNotices] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<Record<string, boolean>>({});

  const trackedRef = useRef(trackedJobs);
  trackedRef.current = trackedJobs;

  const refresh = useCallback(async () => {
    try {
      const list = await api.listRepositories();
      setRepositories(list.items);
      setListError(null);
    } catch (error) {
      setListError(errorMessage(error));
    } finally {
      setLoaded(true);
    }

    const entries = Object.entries(trackedRef.current);
    const results = await Promise.all(
      entries.map(async ([repositoryId, jobId]) => {
        try {
          return [repositoryId, await api.getJob(jobId)] as const;
        } catch {
          return [repositoryId, null] as const;
        }
      }),
    );
    setJobs((current) => {
      const next = { ...current };
      for (const [repositoryId, job] of results) {
        if (job) next[repositoryId] = job;
      }
      return next;
    });
  }, []);

  useEffect(() => {
    saveTrackedJobs(trackedJobs);
  }, [trackedJobs]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const hasActiveWork =
    repositories.some((repo) =>
      ACTIVE_SNAPSHOT_STATUSES.includes(repo.latest_snapshot?.status ?? "ready"),
    ) ||
    Object.entries(trackedJobs).some(([repositoryId]) => {
      const job = jobs[repositoryId];
      return !job || job.status === "queued" || job.status === "running";
    });

  useEffect(() => {
    if (!hasActiveWork) return;
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [hasActiveWork, refresh]);

  function track(repositoryId: string, job: Job | null) {
    if (!job) return;
    setTrackedJobs((current) => ({ ...current, [repositoryId]: job.id }));
    setJobs((current) => ({ ...current, [repositoryId]: job }));
  }

  function setNotice(repositoryId: string, message: string | null) {
    setNotices((current) => {
      const next = { ...current };
      if (message) next[repositoryId] = message;
      else delete next[repositoryId];
      return next;
    });
  }

  async function handleImport(event: FormEvent) {
    event.preventDefault();
    if (!url.trim()) return;
    setSubmitting(true);
    setFormError(null);
    try {
      const result = await api.importRepository(url.trim(), ref.trim() || undefined);
      track(result.repository.id, result.job);
      if (!result.job) {
        setNotice(result.repository.id, "That commit is already indexed.");
      }
      setUrl("");
      setRef("");
      await refresh();
    } catch (error) {
      setFormError(errorMessage(error));
    } finally {
      setSubmitting(false);
    }
  }

  async function handleReindex(repository: Repository, force: boolean) {
    setBusy((current) => ({ ...current, [repository.id]: true }));
    setNotice(repository.id, null);
    try {
      const result = await api.reindexRepository(repository.id, force);
      track(repository.id, result.job);
      if (!result.job) {
        setNotice(
          repository.id,
          `Already indexed at ${result.snapshot.commit_sha.slice(0, 7)}, the head of ${repository.default_branch}.`,
        );
      }
      await refresh();
    } catch (error) {
      setNotice(repository.id, errorMessage(error));
    } finally {
      setBusy((current) => ({ ...current, [repository.id]: false }));
    }
  }

  async function handleCancel(repositoryId: string, jobId: string) {
    setBusy((current) => ({ ...current, [repositoryId]: true }));
    try {
      track(repositoryId, await api.cancelJob(jobId));
      await refresh();
    } catch (error) {
      setNotice(repositoryId, errorMessage(error));
    } finally {
      setBusy((current) => ({ ...current, [repositoryId]: false }));
    }
  }

  const readyCount = repositories.filter(isServable).length;
  const indexingCount = repositories.filter((repo) =>
    ACTIVE_SNAPSHOT_STATUSES.includes(repo.latest_snapshot?.status ?? "ready"),
  ).length;

  return (
    <>
      <div className="section-heading page-heading">
        <div>
          <p className="eyebrow">REPOSITORY INTELLIGENCE</p>
          <h3>Repositories</h3>
          <p>Import a public GitHub repository and CODEMIND indexes a pinned commit of it.</p>
        </div>

        <span className="local-tag">
          {readyCount} READY · {indexingCount} INDEXING
        </span>
      </div>

      <form className="panel import-form" onSubmit={handleImport}>
        <div className="import-fields">
          <label className="field field-grow">
            <span>GitHub repository URL</span>
            <input
              type="url"
              placeholder="https://github.com/owner/repo"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              required
            />
          </label>

          <label className="field">
            <span>Branch, tag or commit (optional)</span>
            <input
              type="text"
              placeholder="default branch"
              value={ref}
              onChange={(event) => setRef(event.target.value)}
            />
          </label>

          <button className="primary-button" type="submit" disabled={submitting}>
            {submitting ? <Loader2 size={16} className="spin" /> : <Plus size={16} />}
            Import repository
          </button>
        </div>

        {formError && <p className="form-error">{formError}</p>}
      </form>

      {listError && (
        <div className="system-note error-note">
          <AlertTriangle size={17} />
          <span>{listError} Start the API server and try again.</span>
        </div>
      )}

      {loaded && !listError && repositories.length === 0 && (
        <article className="panel">
          <div className="empty-state">
            <div className="empty-icon">
              <GitBranch size={28} />
            </div>
            <h4>No repositories yet</h4>
            <p>Paste a GitHub URL above to import your first repository.</p>
          </div>
        </article>
      )}

      <div className="repo-list">
        {repositories.map((repository) => {
          const snapshot = repository.latest_snapshot;
          const trackedJobId = trackedJobs[repository.id];
          const job = jobs[repository.id];
          const jobActive = job && (job.status === "queued" || job.status === "running");
          const snapshotActive = ACTIVE_SNAPSHOT_STATUSES.includes(snapshot?.status ?? "ready");
          const isBusy = busy[repository.id];

          return (
            <article className="panel repo-card" key={repository.id}>
              <div className="repo-main">
                <div className="panel-icon">
                  <GitBranch size={19} />
                </div>

                <div className="repo-title">
                  <a
                    href={`https://github.com/${repositoryName(repository)}`}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {repositoryName(repository)}
                  </a>
                  <div className="repo-meta">
                    <span>{repository.default_branch}</span>
                    {snapshot && (
                      <span className="mono">
                        <GitCommit size={12} />
                        {snapshot.commit_sha.slice(0, 7)}
                      </span>
                    )}
                    {snapshot?.stats.files_included !== undefined && (
                      <span>
                        {snapshot.stats.files_included} of {snapshot.stats.files_total} files indexed
                      </span>
                    )}
                    <span>Indexed {formatDate(snapshot?.indexed_at ?? null)}</span>
                  </div>
                </div>

                <span className={`status-badge ${snapshot?.status ?? "none"}`}>
                  {snapshotActive && <Loader2 size={11} className="spin" />}
                  {snapshot?.status ?? "not indexed"}
                </span>
              </div>

              {jobActive && (
                <div className="job-progress">
                  <div className="job-progress-label">
                    <span>
                      {job.cancel_requested
                        ? "Cancelling…"
                        : job.status === "queued"
                          ? "Queued, waiting for a worker"
                          : (STAGE_LABELS[job.stage ?? ""] ?? job.stage ?? "Running")}
                    </span>
                    <span>{job.progress}%</span>
                  </div>
                  <div className="progress-track">
                    <div className="progress-fill" style={{ width: `${job.progress}%` }} />
                  </div>
                </div>
              )}

              {job?.status === "failed" && job.id === trackedJobId && (
                <p className="form-error">
                  Indexing failed: {job.error_message ?? job.error_code ?? "unknown error"}
                </p>
              )}

              {snapshot && snapshot.warnings.length > 0 && !snapshotActive && (
                <p className="repo-warning">
                  <AlertTriangle size={13} />
                  {snapshot.warnings.length} warning
                  {snapshot.warnings.length === 1 ? "" : "s"} while indexing:{" "}
                  {snapshot.warnings.map((warning) => warning.code).join(", ")}
                </p>
              )}

              {notices[repository.id] && (
                <p className="repo-notice">
                  {notices[repository.id]}
                  {notices[repository.id].startsWith("Already indexed") && (
                    <button
                      type="button"
                      className="link-button"
                      onClick={() => void handleReindex(repository, true)}
                    >
                      Re-index anyway
                    </button>
                  )}
                </p>
              )}

              <div className="repo-actions">
                {jobActive ? (
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={isBusy || job.cancel_requested}
                    onClick={() => void handleCancel(repository.id, job.id)}
                  >
                    <Square size={13} />
                    Cancel
                  </button>
                ) : (
                  <button
                    className="ghost-button"
                    type="button"
                    disabled={isBusy || snapshotActive}
                    onClick={() => void handleReindex(repository, false)}
                  >
                    <RefreshCw size={13} className={isBusy ? "spin" : ""} />
                    Re-index
                  </button>
                )}

                <button
                  className="ghost-button accent"
                  type="button"
                  disabled={!isServable(repository)}
                  title={isServable(repository) ? undefined : "Available once indexing finishes"}
                  onClick={() => onAsk(repository.id)}
                >
                  <Bot size={13} />
                  Ask AI Engineer
                </button>
              </div>
            </article>
          );
        })}
      </div>
    </>
  );
}
