import { type FormEvent, useEffect, useState } from "react";

import {
  AlertTriangle,
  Check,
  Download,
  FlaskConical,
  Loader2,
  Wand2,
  X,
} from "lucide-react";

import {
  ApiError,
  type Patch,
  type PatchSummary,
  TEST_PROFILES,
  type TestRun,
  type TestRunStatus,
  api,
} from "../api";

const POLL_MS = 2000;
const UNFINISHED: TestRunStatus[] = ["queued", "running"];

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong.";
}

const STATUS_LABEL: Record<string, string> = {
  validated: "ready for review",
  invalid: "invalid",
  failed: "failed",
  approved: "approved",
  rejected: "rejected",
  exported: "exported",
  queued: "queued",
  running: "running",
  passed: "passed",
  no_tests: "no tests",
  timed_out: "timed out",
  error: "error",
  blocked: "not run",
};

function StatusPill({ status }: { status: string }) {
  return <span className={`patch-status ${status}`}>{STATUS_LABEL[status] ?? status}</span>;
}

/** Propose a fix for an investigation, review its diff, test it, and approve or reject it. */
export default function PatchPanel({ investigationId }: { investigationId: string }) {
  const [instruction, setInstruction] = useState("");
  const [proposing, setProposing] = useState(false);
  const [patches, setPatches] = useState<PatchSummary[]>([]);
  const [patch, setPatch] = useState<Patch | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setPatch(null);
    setError(null);
    setInstruction("");
    api
      .listPatches(investigationId)
      .then((list) => setPatches(list.items))
      .catch(() => setPatches([]));
  }, [investigationId]);

  function remember(next: Patch) {
    setPatch(next);
    setPatches((current) => {
      const summary: PatchSummary = {
        id: next.id,
        instruction: next.instruction,
        source: next.source,
        status: next.status,
        files: next.files,
        created_at: next.created_at,
      };
      const rest = current.filter((item) => item.id !== next.id);
      return current.some((item) => item.id === next.id)
        ? current.map((item) => (item.id === next.id ? summary : item))
        : [summary, ...rest];
    });
  }

  async function propose(event: FormEvent) {
    event.preventDefault();
    if (instruction.trim().length < 3) return;
    setProposing(true);
    setError(null);
    try {
      remember(await api.proposePatch(investigationId, instruction.trim()));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setProposing(false);
    }
  }

  async function open(id: string) {
    setError(null);
    try {
      setPatch(await api.getPatch(id));
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <article className="panel patch-panel">
      <div className="panel-heading">
        <div>
          <h3>Propose a fix</h3>
          <p>The model drafts edits; you review the diff. Nothing is written to your repository.</p>
        </div>
        <Wand2 size={18} />
      </div>

      <form className="patch-form" onSubmit={propose}>
        <input
          type="text"
          placeholder="What should change? e.g. Retry failed jobs at most three times"
          value={instruction}
          onChange={(event) => setInstruction(event.target.value)}
          maxLength={2000}
        />
        <button
          className="ghost-button accent"
          type="submit"
          disabled={proposing || instruction.trim().length < 3}
        >
          {proposing ? <Loader2 size={13} className="spin" /> : <Wand2 size={13} />}
          {proposing ? "Drafting…" : "Draft patch"}
        </button>
      </form>

      {error && (
        <div className="system-note error-note">
          <AlertTriangle size={17} />
          <span>{error}</span>
        </div>
      )}

      {patches.length > 0 && (
        <ul className="patch-list">
          {patches.map((item) => (
            <li key={item.id}>
              <button
                type="button"
                className={patch?.id === item.id ? "active" : ""}
                onClick={() => void open(item.id)}
              >
                <span>{item.instruction}</span>
                <StatusPill status={item.status} />
              </button>
            </li>
          ))}
        </ul>
      )}

      {patch && <PatchView key={patch.id} patch={patch} onChange={remember} />}
    </article>
  );
}

function PatchView({ patch, onChange }: { patch: Patch; onChange: (patch: Patch) => void }) {
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function act(name: string, action: () => Promise<void>) {
    setBusy(name);
    setError(null);
    try {
      await action();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(null);
    }
  }

  const approve = () =>
    act("approve", async () => onChange(await api.approvePatch(patch.id, patch.diff_sha256!)));
  const reject = () => act("reject", async () => onChange(await api.rejectPatch(patch.id)));
  const download = () =>
    act("export", async () => {
      const exported = await api.exportPatch(patch.id);
      const url = URL.createObjectURL(new Blob([exported.diff], { type: "text/x-diff" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = exported.filename;
      link.click();
      URL.revokeObjectURL(url);
      onChange(await api.getPatch(patch.id));
    });

  const { errors, warnings } = patch.validation;
  const canTest = ["validated", "approved", "exported"].includes(patch.status);

  return (
    <div className="patch-view">
      <div className="patch-head">
        <StatusPill status={patch.status} />
        <span className="mono">
          {patch.source === "model" ? "drafted by the model" : "your edits"} · commit{" "}
          {patch.commit_sha.slice(0, 7)}
        </span>
      </div>

      {patch.summary && <p className="answer-summary">{patch.summary}</p>}

      {patch.status === "failed" && (
        <p className="patch-error">{patch.error_message ?? patch.error_code}</p>
      )}

      {errors.length > 0 && (
        <div className="answer-section">
          <h4>Why it was not accepted</h4>
          <ul className="issue-list">
            {errors.map((issue, index) => (
              <li key={index}>
                <span className="mono">
                  {issue.edit_index !== null ? `edit ${issue.edit_index + 1}` : "patch"}
                  {issue.path ? ` · ${issue.path}` : ""}
                </span>{" "}
                {issue.message}
              </li>
            ))}
          </ul>
        </div>
      )}

      {warnings.length > 0 && (
        <ul className="issue-list warnings">
          {warnings.map((issue, index) => (
            <li key={index}>{issue.message}</li>
          ))}
        </ul>
      )}

      {patch.files.length > 0 && (
        <ul className="patch-files mono">
          {patch.files.map((file) => (
            <li key={file.path}>
              <span>{file.path}</span>
              {file.status === "added" && <small>new</small>}
              <span className="added">+{file.additions}</span>
              <span className="removed">−{file.deletions}</span>
            </li>
          ))}
        </ul>
      )}

      {patch.diff && <DiffView diff={patch.diff} />}

      {patch.limitations.length > 0 && (
        <div className="answer-section">
          <h4>Limitations</h4>
          <ul>
            {patch.limitations.map((item, index) => (
              <li key={index}>{item}</li>
            ))}
          </ul>
        </div>
      )}

      {canTest && <TestRuns patchId={patch.id} />}

      {error && (
        <div className="system-note error-note">
          <AlertTriangle size={17} />
          <span>{error}</span>
        </div>
      )}

      <div className="patch-actions">
        {patch.status === "validated" && (
          <button
            type="button"
            className="ghost-button accent"
            disabled={busy !== null}
            onClick={() => void approve()}
            title="Approve exactly this diff"
          >
            {busy === "approve" ? <Loader2 size={13} className="spin" /> : <Check size={13} />}
            Approve diff
          </button>
        )}
        {(patch.status === "approved" || patch.status === "exported") && (
          <button
            type="button"
            className="ghost-button accent"
            disabled={busy !== null}
            onClick={() => void download()}
          >
            {busy === "export" ? <Loader2 size={13} className="spin" /> : <Download size={13} />}
            Download .diff
          </button>
        )}
        {["validated", "invalid", "approved"].includes(patch.status) && (
          <button
            type="button"
            className="ghost-button"
            disabled={busy !== null}
            onClick={() => void reject()}
          >
            <X size={13} />
            Reject
          </button>
        )}
      </div>
      {(patch.status === "approved" || patch.status === "exported") && (
        <p className="muted-line">
          Apply it on a checkout of commit {patch.commit_sha.slice(0, 7)} with{" "}
          <span className="mono">git apply</span>.
        </p>
      )}
    </div>
  );
}

function DiffView({ diff }: { diff: string }) {
  const lines = diff.replace(/\n$/, "").split("\n");
  return (
    <pre className="diff-view">
      {lines.map((line, index) => {
        let kind = "";
        if (line.startsWith("diff --git") || line.startsWith("+++") || line.startsWith("---"))
          kind = "meta";
        else if (line.startsWith("@@")) kind = "hunk";
        else if (line.startsWith("+")) kind = "add";
        else if (line.startsWith("-")) kind = "del";
        return (
          <span key={index} className={`diff-line ${kind}`}>
            {line || " "}
          </span>
        );
      })}
    </pre>
  );
}

function TestRuns({ patchId }: { patchId: string }) {
  const [runs, setRuns] = useState<TestRun[]>([]);
  const [profile, setProfile] = useState<string>(TEST_PROFILES[0]);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);

  useEffect(() => {
    api
      .listTestRuns(patchId)
      .then((list) => setRuns(list.items))
      .catch(() => setRuns([]));
  }, [patchId]);

  const pending = runs.filter((run) => UNFINISHED.includes(run.status)).map((run) => run.id);
  const pendingKey = pending.join(",");

  useEffect(() => {
    if (!pending.length) return;
    const timer = window.setInterval(async () => {
      const updated = await Promise.all(pending.map((id) => api.getTestRun(id).catch(() => null)));
      setRuns((current) =>
        current.map((run) => updated.find((next) => next?.id === run.id) ?? run),
      );
    }, POLL_MS);
    return () => window.clearInterval(timer);
    // Re-arm only when the set of unfinished runs changes.
  }, [pendingKey]);

  async function start() {
    setStarting(true);
    setError(null);
    try {
      const run = await api.runTests(patchId, profile);
      setRuns((current) => [run, ...current]);
      setOpenId(run.id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setStarting(false);
    }
  }

  return (
    <div className="answer-section test-runs">
      <h4>Tests in a sandbox</h4>
      <div className="test-run-start">
        <select value={profile} onChange={(event) => setProfile(event.target.value)}>
          {TEST_PROFILES.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        <button
          type="button"
          className="ghost-button"
          disabled={starting}
          onClick={() => void start()}
        >
          {starting ? <Loader2 size={13} className="spin" /> : <FlaskConical size={13} />}
          Run tests
        </button>
      </div>
      {error && <p className="patch-error">{error}</p>}
      {runs.length > 0 && (
        <ul className="test-run-list">
          {runs.map((run) => (
            <li key={run.id}>
              <button
                type="button"
                className="test-run-row"
                onClick={() => setOpenId(openId === run.id ? null : run.id)}
              >
                <StatusPill status={run.status} />
                {UNFINISHED.includes(run.status) && <Loader2 size={12} className="spin" />}
                <span className="mono">{run.profile}</span>
                <small>
                  {run.exit_code !== null ? `exit ${run.exit_code}` : ""}
                  {run.duration_ms !== null ? ` · ${(run.duration_ms / 1000).toFixed(1)}s` : ""}
                </small>
              </button>
              {openId === run.id && (
                <div className="test-run-detail">
                  {run.error_message && <p className="patch-error">{run.error_message}</p>}
                  <p className="mono muted-command">$ {run.command.join(" ")}</p>
                  {run.output && (
                    <pre className="code-snippet test-output">
                      {run.output_truncated ? "…\n" : ""}
                      {run.output}
                    </pre>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
