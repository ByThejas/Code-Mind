import { useEffect, useState } from "react";

import { Loader2, X } from "lucide-react";

import { ApiError, type FileContent, api } from "../api";

const CONTEXT_LINES = 6;

export interface SourceTarget {
  path: string;
  startLine: number;
  endLine: number;
  label?: string | null;
  snapshotId?: string;
}

/** A file excerpt from the indexed snapshot, with the cited lines highlighted. */
export default function SourceViewer({
  repositoryId,
  target,
  onClose,
}: {
  repositoryId: string;
  target: SourceTarget;
  onClose: () => void;
}) {
  const { path, startLine, endLine, label, snapshotId } = target;
  const [content, setContent] = useState<FileContent | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setContent(null);
    setError(null);
    api
      .readFile(
        repositoryId,
        path,
        Math.max(1, startLine - CONTEXT_LINES),
        endLine + CONTEXT_LINES,
        snapshotId,
      )
      .then((result) => !cancelled && setContent(result))
      .catch(
        (err) =>
          !cancelled &&
          setError(err instanceof ApiError ? err.message : "Something went wrong."),
      );
    return () => {
      cancelled = true;
    };
  }, [repositoryId, path, startLine, endLine, snapshotId]);

  const lines = content ? content.text.replace(/\n$/, "").split("\n") : [];

  return (
    <article className="panel source-viewer">
      <div className="panel-heading">
        <div className="source-title">
          <h3 className="mono">{path}</h3>
          <p>
            {startLine === endLine ? `Line ${startLine}` : `Lines ${startLine}–${endLine}`}
            {label ? ` · ${label}` : ""}
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
            const cited = number >= startLine && number <= endLine;
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
