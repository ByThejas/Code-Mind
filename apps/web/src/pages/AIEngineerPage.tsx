import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  AlertTriangle,
  Bot,
  FileCode2,
  History,
  Loader2,
  Search,
  Send,
  X,
} from "lucide-react";

import {
  ApiError,
  type Evidence,
  type FileContent,
  type Investigation,
  type InvestigationSummary,
  type Repository,
  api,
  isServable,
  repositoryName,
} from "../api";

const VIEWER_CONTEXT_LINES = 6;

type Mode = "ask" | "search";

interface SourceTarget {
  evidence: Evidence;
  snapshotId: string;
}

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong.";
}

function location(evidence: Evidence): string {
  return evidence.start_line === evidence.end_line
    ? `${evidence.path}:${evidence.start_line}`
    : `${evidence.path}:${evidence.start_line}-${evidence.end_line}`;
}

interface AIEngineerPageProps {
  repositoryId: string | null;
  onSelectRepository: (repositoryId: string) => void;
}

export default function AIEngineerPage({
  repositoryId,
  onSelectRepository,
}: AIEngineerPageProps) {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [reposError, setReposError] = useState<string | null>(null);

  const [mode, setMode] = useState<Mode>("ask");
  const [question, setQuestion] = useState("");
  const [asking, setAsking] = useState(false);
  const [investigation, setInvestigation] = useState<Investigation | null>(null);
  const [history, setHistory] = useState<InvestigationSummary[]>([]);

  const [searchQuery, setSearchQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [searchResults, setSearchResults] = useState<{
    snapshotId: string;
    items: Evidence[];
  } | null>(null);

  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<SourceTarget | null>(null);

  useEffect(() => {
    api
      .listRepositories()
      .then((list) => {
        setRepositories(list.items);
        setReposError(null);
        if (!repositoryId) {
          const first = list.items.find(isServable);
          if (first) onSelectRepository(first.id);
        }
      })
      .catch((err) => setReposError(errorMessage(err)));
    // Load once; the selected repository is owned by the parent.
  }, []);

  useEffect(() => {
    setInvestigation(null);
    setSearchResults(null);
    setSource(null);
    setError(null);
    setHistory([]);
    if (!repositoryId) return;
    api
      .listInvestigations(repositoryId)
      .then((list) => setHistory(list.items))
      .catch(() => setHistory([]));
  }, [repositoryId]);

  const repository = repositories.find((repo) => repo.id === repositoryId) ?? null;

  async function handleAsk(event: FormEvent) {
    event.preventDefault();
    if (!repositoryId || question.trim().length < 3) return;
    setAsking(true);
    setError(null);
    setSource(null);
    try {
      const result = await api.ask(repositoryId, question.trim());
      setInvestigation(result);
      setHistory((current) => [
        {
          id: result.id,
          snapshot_id: result.snapshot_id,
          question: result.question,
          status: result.status,
          created_at: result.created_at,
        },
        ...current.filter((item) => item.id !== result.id),
      ]);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setAsking(false);
    }
  }

  async function handleSearch(event: FormEvent) {
    event.preventDefault();
    if (!repositoryId || searchQuery.trim().length < 2) return;
    setSearching(true);
    setError(null);
    setSource(null);
    try {
      const result = await api.search(repositoryId, searchQuery.trim(), 20);
      setSearchResults({ snapshotId: result.snapshot_id, items: result.items });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSearching(false);
    }
  }

  async function openInvestigation(id: string) {
    setError(null);
    setSource(null);
    setMode("ask");
    try {
      const result = await api.getInvestigation(id);
      setInvestigation(result);
      setQuestion(result.question);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <>
      <div className="section-heading page-heading">
        <div>
          <p className="eyebrow">GROUNDED ANSWERS</p>
          <h3>AI Engineer</h3>
          <p>Ask about a repository. Every claim links to the file and lines it came from.</p>
        </div>

        <label className="repo-select">
          <span>Repository</span>
          <select
            value={repositoryId ?? ""}
            onChange={(event) => onSelectRepository(event.target.value)}
          >
            <option value="" disabled>
              {repositories.length ? "Choose a repository" : "No repositories yet"}
            </option>
            {repositories.map((repo) => (
              <option key={repo.id} value={repo.id}>
                {repositoryName(repo)}
                {isServable(repo) ? "" : ` (${repo.latest_snapshot?.status ?? "not indexed"})`}
              </option>
            ))}
          </select>
        </label>
      </div>

      {reposError && (
        <div className="system-note error-note">
          <AlertTriangle size={17} />
          <span>{reposError} Start the API server and try again.</span>
        </div>
      )}

      <div className="engineer-grid">
        <div className="engineer-main">
          <div className="mode-tabs" role="tablist">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "ask"}
              className={mode === "ask" ? "active" : ""}
              onClick={() => setMode("ask")}
            >
              <Bot size={14} />
              Ask a question
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "search"}
              className={mode === "search" ? "active" : ""}
              onClick={() => setMode("search")}
            >
              <Search size={14} />
              Search code
            </button>
          </div>

          {mode === "ask" ? (
            <form className="panel ask-form" onSubmit={handleAsk}>
              <textarea
                placeholder={
                  repository
                    ? `How does ${repository.name} handle authentication?`
                    : "Choose a repository first"
                }
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                onKeyDown={(event) => {
                  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                    event.currentTarget.form?.requestSubmit();
                  }
                }}
                rows={3}
                maxLength={2000}
                disabled={!repositoryId}
              />
              <div className="ask-footer">
                <span>Ctrl + Enter to send. Answers come from a local model and can take a minute.</span>
                <button
                  className="primary-button"
                  type="submit"
                  disabled={!repositoryId || asking || question.trim().length < 3}
                >
                  {asking ? <Loader2 size={15} className="spin" /> : <Send size={15} />}
                  {asking ? "Investigating…" : "Ask"}
                </button>
              </div>
            </form>
          ) : (
            <form className="panel search-form" onSubmit={handleSearch}>
              <Search size={16} />
              <input
                type="search"
                placeholder="Function names, paths, or plain words"
                value={searchQuery}
                onChange={(event) => setSearchQuery(event.target.value)}
                minLength={2}
                disabled={!repositoryId}
              />
              <button
                className="primary-button"
                type="submit"
                disabled={!repositoryId || searching || searchQuery.trim().length < 2}
              >
                {searching ? <Loader2 size={15} className="spin" /> : <Search size={15} />}
                Search
              </button>
            </form>
          )}

          {error && (
            <div className="system-note error-note">
              <AlertTriangle size={17} />
              <span>{error}</span>
            </div>
          )}

          {mode === "ask" && investigation && (
            <AnswerView
              investigation={investigation}
              onOpenSource={(evidence) =>
                setSource({ evidence, snapshotId: investigation.snapshot_id })
              }
            />
          )}

          {mode === "search" && searchResults && (
            <div className="evidence-list">
              {searchResults.items.length === 0 && (
                <p className="muted-line">No matching code found.</p>
              )}
              {searchResults.items.map((evidence) => (
                <EvidenceCard
                  key={evidence.id}
                  evidence={evidence}
                  onOpen={() =>
                    setSource({ evidence, snapshotId: searchResults.snapshotId })
                  }
                />
              ))}
            </div>
          )}
        </div>

        <aside className="engineer-side">
          {source && repositoryId ? (
            <SourceViewer
              repositoryId={repositoryId}
              target={source}
              onClose={() => setSource(null)}
            />
          ) : (
            <article className="panel history-panel">
              <div className="panel-heading">
                <div>
                  <h3>Recent questions</h3>
                  <p>Past investigations on this repository.</p>
                </div>
                <History size={18} />
              </div>
              {history.length === 0 ? (
                <p className="muted-line">Nothing asked yet.</p>
              ) : (
                <ul className="history-list">
                  {history.map((item) => (
                    <li key={item.id}>
                      <button
                        type="button"
                        className={investigation?.id === item.id ? "active" : ""}
                        onClick={() => void openInvestigation(item.id)}
                      >
                        <span>{item.question}</span>
                        <small>
                          {item.status === "failed" ? "Failed · " : ""}
                          {new Date(item.created_at).toLocaleString(undefined, {
                            dateStyle: "short",
                            timeStyle: "short",
                          })}
                        </small>
                      </button>
                    </li>
                  ))}
                </ul>
              )}
            </article>
          )}
        </aside>
      </div>
    </>
  );
}

function AnswerView({
  investigation,
  onOpenSource,
}: {
  investigation: Investigation;
  onOpenSource: (evidence: Evidence) => void;
}) {
  const byId = useMemo(
    () => new Map(investigation.evidence.map((evidence) => [evidence.id, evidence])),
    [investigation],
  );
  const answer = investigation.answer;

  if (investigation.status === "failed" || !answer) {
    return (
      <div className="system-note error-note">
        <AlertTriangle size={17} />
        <span>
          The investigation failed:{" "}
          {investigation.error_message ?? investigation.error_code ?? "no answer was produced"}
        </span>
      </div>
    );
  }

  return (
    <article className="panel answer-panel">
      <p className="answer-question">{investigation.question}</p>
      <p className="answer-summary">{answer.summary}</p>

      {answer.claims.length > 0 && (
        <ol className="claim-list">
          {answer.claims.map((claim, index) => (
            <li key={index} className={`claim ${claim.status}`}>
              <div className="claim-text">
                <span className={`claim-status ${claim.status}`}>{claim.status}</span>
                {claim.text}
              </div>
              {claim.evidence_ids.length > 0 && (
                <div className="citations">
                  {claim.evidence_ids.map((id) => {
                    const evidence = byId.get(id);
                    if (!evidence) return null;
                    return (
                      <button
                        key={id}
                        type="button"
                        className="citation"
                        onClick={() => onOpenSource(evidence)}
                        title={evidence.symbol_name ?? undefined}
                      >
                        <FileCode2 size={12} />
                        {location(evidence)}
                      </button>
                    );
                  })}
                </div>
              )}
            </li>
          ))}
        </ol>
      )}

      {answer.limitations.length > 0 && (
        <div className="answer-section">
          <h4>Limitations</h4>
          <ul>
            {answer.limitations.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {answer.suggested_next_steps.length > 0 && (
        <div className="answer-section">
          <h4>Suggested next steps</h4>
          <ul>
            {answer.suggested_next_steps.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      <div className="answer-section">
        <h4>Evidence ({investigation.evidence.length})</h4>
        <div className="evidence-chips">
          {investigation.evidence.map((evidence) => (
            <button
              key={evidence.id}
              type="button"
              className="citation subtle"
              onClick={() => onOpenSource(evidence)}
            >
              {location(evidence)}
            </button>
          ))}
        </div>
      </div>

      <p className="answer-footer mono">
        Commit {investigation.commit_sha.slice(0, 7)}
      </p>
    </article>
  );
}

function EvidenceCard({ evidence, onOpen }: { evidence: Evidence; onOpen: () => void }) {
  return (
    <button type="button" className="panel evidence-card" onClick={onOpen}>
      <div className="evidence-head">
        <span className="mono">{location(evidence)}</span>
        {evidence.symbol_name && <span className="symbol">{evidence.symbol_name}</span>}
        <span className="retrieved-by">{evidence.retrieved_by.join(" · ")}</span>
      </div>
      <pre className="code-snippet">
        {evidence.text.split("\n").slice(0, 8).join("\n")}
      </pre>
    </button>
  );
}

function SourceViewer({
  repositoryId,
  target,
  onClose,
}: {
  repositoryId: string;
  target: SourceTarget;
  onClose: () => void;
}) {
  const { evidence, snapshotId } = target;
  const [content, setContent] = useState<FileContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setContent(null);
    setError(null);
    api
      .readFile(
        repositoryId,
        evidence.path,
        Math.max(1, evidence.start_line - VIEWER_CONTEXT_LINES),
        evidence.end_line + VIEWER_CONTEXT_LINES,
        snapshotId,
      )
      .then((result) => !cancelled && setContent(result))
      .catch((err) => !cancelled && setError(errorMessage(err)));
    return () => {
      cancelled = true;
    };
  }, [repositoryId, evidence, snapshotId]);

  const lines = content ? content.text.replace(/\n$/, "").split("\n") : [];

  return (
    <article className="panel source-viewer">
      <div className="panel-heading">
        <div className="source-title">
          <h3 className="mono">{evidence.path}</h3>
          <p>
            Lines {evidence.start_line}–{evidence.end_line}
            {evidence.symbol_name ? ` · ${evidence.symbol_name}` : ""}
          </p>
        </div>
        <button className="icon-button" type="button" aria-label="Close" onClick={onClose}>
          <X size={16} />
        </button>
      </div>

      {error && <p className="form-error">{error}</p>}
      {!content && !error && (
        <p className="muted-line">
          <Loader2 size={13} className="spin" /> Loading source…
        </p>
      )}
      {content && (
        <pre className="code-view">
          {lines.map((line, index) => {
            const number = content.start_line + index;
            const cited = number >= evidence.start_line && number <= evidence.end_line;
            return (
              <div key={number} className={`code-line ${cited ? "cited" : ""}`}>
                <span className="line-number">{number}</span>
                <span className="line-text">{line || " "}</span>
              </div>
            );
          })}
        </pre>
      )}
    </article>
  );
}
