import { type FormEvent, useEffect, useMemo, useState } from "react";

import {
  AlertTriangle,
  ArrowLeft,
  ArrowRight,
  Boxes,
  ChevronRight,
  FileCode2,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";

import {
  ApiError,
  type CodeSymbol,
  type GraphEdge,
  type GraphInfo,
  type GraphNode,
  type GraphResponse,
  type Repository,
  type SymbolDetail,
  api,
  isServable,
  repositoryName,
} from "../api";
import SourceViewer, { type SourceTarget } from "../components/SourceViewer";
import { layeredLayout } from "../lib/layeredLayout";

type Mode = "modules" | "symbols";

// Edges below this confidence are name-only guesses (docs/graph-design.md §3).
const GUESS_THRESHOLD = 0.6;
const FOCUS_SIDE_LIMIT = 10;

function errorMessage(error: unknown): string {
  return error instanceof ApiError ? error.message : "Something went wrong.";
}

function plural(count: number, word: string): string {
  return `${count.toLocaleString()} ${word}${count === 1 ? "" : "s"}`;
}

function symbolIdOf(nodeId: string): string | null {
  return nodeId.startsWith("symbol:") ? nodeId.slice("symbol:".length) : null;
}

interface ArchitecturePageProps {
  repositoryId: string | null;
  onSelectRepository: (repositoryId: string) => void;
}

export default function ArchitecturePage({
  repositoryId,
  onSelectRepository,
}: ArchitecturePageProps) {
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [reposError, setReposError] = useState<string | null>(null);
  const [mode, setMode] = useState<Mode>("modules");
  const [source, setSource] = useState<SourceTarget | null>(null);
  const [graphInfo, setGraphInfo] = useState<GraphInfo | null>(null);

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
    setSource(null);
    setGraphInfo(null);
  }, [repositoryId]);

  const repository = repositories.find((repo) => repo.id === repositoryId) ?? null;
  const ready = repository !== null && isServable(repository);

  return (
    <>
      <div className="section-heading page-heading">
        <div>
          <p className="eyebrow">CODE GRAPH</p>
          <h3>Architecture</h3>
          <p>
            How the code connects: which parts import which, and which functions call which.
          </p>
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

      {repository && !ready && (
        <p className="muted-line">
          This repository has no finished index yet. Index it on the Repositories page first.
        </p>
      )}

      {repositoryId && ready && graphInfo && !graphInfo.available && (
        <GraphMissingNote repositoryId={repositoryId} />
      )}

      {repositoryId && ready && (
        <div className={`engineer-grid architecture-grid ${source ? "" : "single"}`}>
          <div className="engineer-main">
            <div className="mode-tabs" role="tablist">
              <button
                type="button"
                role="tab"
                aria-selected={mode === "modules"}
                className={mode === "modules" ? "active" : ""}
                onClick={() => setMode("modules")}
              >
                <Boxes size={14} />
                Module map
              </button>
              <button
                type="button"
                role="tab"
                aria-selected={mode === "symbols"}
                className={mode === "symbols" ? "active" : ""}
                onClick={() => setMode("symbols")}
              >
                <Search size={14} />
                Symbols
              </button>
            </div>

            {mode === "modules" ? (
              <ModuleExplorer
                key={repositoryId}
                repositoryId={repositoryId}
                onGraphInfo={setGraphInfo}
                onOpenSource={setSource}
              />
            ) : (
              <SymbolExplorer
                key={repositoryId}
                repositoryId={repositoryId}
                onGraphInfo={setGraphInfo}
                onOpenSource={setSource}
              />
            )}
          </div>

          {source && (
            <aside className="engineer-side">
              <SourceViewer
                repositoryId={repositoryId}
                target={source}
                onClose={() => setSource(null)}
              />
            </aside>
          )}
        </div>
      )}
    </>
  );
}

function GraphMissingNote({ repositoryId }: { repositoryId: string }) {
  const [state, setState] = useState<"idle" | "busy" | "queued" | string>("idle");

  async function reindex() {
    setState("busy");
    try {
      await api.reindexRepository(repositoryId, true);
      setState("queued");
    } catch (err) {
      setState(errorMessage(err));
    }
  }

  return (
    <div className="system-note">
      <AlertTriangle size={17} />
      <span>
        {state === "queued"
          ? "Re-index started. Follow it on the Repositories page, then come back."
          : state !== "idle" && state !== "busy"
            ? state
            : "This snapshot was indexed before the code graph existed. Re-index it to build the graph."}
      </span>
      {(state === "idle" || state === "busy") && (
        <button
          className="ghost-button"
          type="button"
          disabled={state === "busy"}
          onClick={() => void reindex()}
        >
          {state === "busy" ? <Loader2 size={13} className="spin" /> : <RefreshCw size={13} />}
          Re-index
        </button>
      )}
    </div>
  );
}

/* ---------- Module map ---------- */

const NODE_WIDTH = 176;
const NODE_HEIGHT = 42;
const COLUMN_GAP = 92;
const ROW_GAP = 14;
const PADDING = 18;
const AUTO_DEPTHS = [2, 3, 4];
// Wide maps shrink to fit the panel, down to this scale; beyond it the canvas scrolls.
const MIN_SCALE = 0.72;
const LAYER_GAP = 56;
const FIT_WIDTH = 1080;
const FIT_HEIGHT = 620;

function ModuleExplorer({
  repositoryId,
  onGraphInfo,
  onOpenSource,
}: {
  repositoryId: string;
  onGraphInfo: (info: GraphInfo) => void;
  onOpenSource: (target: SourceTarget) => void;
}) {
  // null = automatic: the shallowest level of detail where modules connect to each other.
  const [depth, setDepth] = useState<number | null>(null);
  const [shownDepth, setShownDepth] = useState(AUTO_DEPTHS[0]);
  const [prefix, setPrefix] = useState<string | null>(null);
  const [data, setData] = useState<GraphResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [hovered, setHovered] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    const load = async () => {
      if (depth !== null) return [depth, await api.moduleGraph(repositoryId, depth, prefix)] as const;
      let level = AUTO_DEPTHS[0];
      let result = await api.moduleGraph(repositoryId, level, prefix);
      for (const next of AUTO_DEPTHS.slice(1)) {
        if (result.edges.length > 0 || cancelled) break;
        const deeper = await api.moduleGraph(repositoryId, next, prefix);
        if (deeper.nodes.length === result.nodes.length) break;
        [level, result] = [next, deeper];
      }
      return [level, result] as const;
    };
    load()
      .then(([level, result]) => {
        if (cancelled) return;
        setData(result);
        setShownDepth(level);
        onGraphInfo(result.graph);
        setSelected(null);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [repositoryId, depth, prefix]);

  const layout = useMemo(() => {
    if (!data) return null;
    const { placements, columns } = layeredLayout(
      data.nodes.map((node) => node.id),
      data.edges,
    );
    const widest = Math.max(1, ...columns.map((column) => column.length));
    const layers = columns.length;
    // Layers run left to right when that fits the panel, otherwise top to bottom.
    const across = {
      width: PADDING * 2 + layers * NODE_WIDTH + (layers - 1) * COLUMN_GAP,
      height: PADDING * 2 + widest * NODE_HEIGHT + (widest - 1) * ROW_GAP,
    };
    const down = {
      width: PADDING * 2 + widest * NODE_WIDTH + (widest - 1) * ROW_GAP,
      height: PADDING * 2 + layers * NODE_HEIGHT + (layers - 1) * LAYER_GAP,
    };
    const overflow = (size: { width: number; height: number }) =>
      Math.max(size.width / FIT_WIDTH, size.height / FIT_HEIGHT);
    const vertical = across.width > FIT_WIDTH && overflow(down) < overflow(across);
    const { width, height } = vertical ? down : across;
    const boxes = new Map<string, { x: number; y: number }>();
    for (const [id, { column, row }] of placements) {
      const count = columns[column].length;
      if (vertical) {
        const rowWidth = count * NODE_WIDTH + (count - 1) * ROW_GAP;
        boxes.set(id, {
          x: (width - rowWidth) / 2 + row * (NODE_WIDTH + ROW_GAP),
          y: PADDING + column * (NODE_HEIGHT + LAYER_GAP),
        });
      } else {
        const columnHeight = count * NODE_HEIGHT + (count - 1) * ROW_GAP;
        boxes.set(id, {
          x: PADDING + column * (NODE_WIDTH + COLUMN_GAP),
          y: (height - columnHeight) / 2 + row * (NODE_HEIGHT + ROW_GAP),
        });
      }
    }
    return { boxes, width, height, vertical };
  }, [data]);

  const nodesById = useMemo(
    () => new Map((data?.nodes ?? []).map((node) => [node.id, node])),
    [data],
  );

  const focus = hovered ?? selected;
  const linked = useMemo(() => {
    if (!data || !focus) return null;
    const ids = new Set([focus]);
    for (const edge of data.edges) {
      if (edge.source === focus) ids.add(edge.target);
      if (edge.target === focus) ids.add(edge.source);
    }
    return ids;
  }, [data, focus]);

  const selectedNode = selected ? nodesById.get(selected) ?? null : null;
  const crumbs = prefix ? prefix.split("/") : [];

  return (
    <>
      <div className="panel graph-panel">
        <div className="graph-toolbar">
          <nav className="crumbs" aria-label="Module path">
            <button type="button" onClick={() => setPrefix(null)} disabled={!prefix}>
              Repository
            </button>
            {crumbs.map((part, index) => (
              <span key={index} className="crumb">
                <ChevronRight size={12} />
                <button
                  type="button"
                  disabled={index === crumbs.length - 1}
                  onClick={() => setPrefix(crumbs.slice(0, index + 1).join("/"))}
                >
                  {part}
                </button>
              </span>
            ))}
          </nav>

          <label className="inline-select">
            <span>Detail</span>
            <select
              value={depth ?? "auto"}
              onChange={(event) =>
                setDepth(event.target.value === "auto" ? null : Number(event.target.value))
              }
            >
              <option value="auto">Auto ({shownDepth})</option>
              {[1, 2, 3, 4, 5].map((level) => (
                <option key={level} value={level}>
                  {level} {level === 1 ? "level" : "levels"}
                </option>
              ))}
            </select>
          </label>
        </div>

        {error && <p className="form-error">{error}</p>}
        {loading && !data && (
          <p className="muted-line">
            <Loader2 size={13} className="spin" /> Building the map…
          </p>
        )}

        {data && layout && data.nodes.length === 0 && (
          <p className="muted-line">No source files under this path.</p>
        )}

        {data && layout && data.nodes.length > 0 && (
          <div className={`graph-canvas ${loading ? "loading" : ""}`}>
            <svg
              viewBox={`0 0 ${layout.width} ${layout.height}`}
              style={{ width: "100%", minWidth: layout.width * MIN_SCALE, maxWidth: layout.width }}
              role="img"
              aria-label="Module dependency map"
              onClick={() => setSelected(null)}
            >
              <defs>
                <marker
                  id="arrow"
                  viewBox="0 0 8 8"
                  refX="7"
                  refY="4"
                  markerWidth="6"
                  markerHeight="6"
                  orient="auto-start-reverse"
                >
                  <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
                </marker>
              </defs>

              {data.edges.map((edge) => {
                const from = layout.boxes.get(edge.source);
                const to = layout.boxes.get(edge.target);
                if (!from || !to) return null;
                const active = focus !== null && (edge.source === focus || edge.target === focus);
                const dim = focus !== null && !active;
                return (
                  <path
                    key={edge.id}
                    className={`graph-edge ${active ? "active" : ""} ${dim ? "dim" : ""}`}
                    d={edgePath(from, to, layout.vertical)}
                    strokeWidth={1 + Math.log2(edge.count)}
                    markerEnd="url(#arrow)"
                  >
                    <title>
                      {`${nodesById.get(edge.source)?.path} imports ${nodesById.get(edge.target)?.path} (${plural(edge.count, "import")})`}
                    </title>
                  </path>
                );
              })}

              {data.nodes.map((node) => {
                const box = layout.boxes.get(node.id);
                if (!box) return null;
                const dim = linked !== null && !linked.has(node.id);
                return (
                  <g
                    key={node.id}
                    className={`graph-node ${selected === node.id ? "selected" : ""} ${dim ? "dim" : ""}`}
                    transform={`translate(${box.x}, ${box.y})`}
                    onMouseEnter={() => setHovered(node.id)}
                    onMouseLeave={() => setHovered(null)}
                    onClick={(event) => {
                      event.stopPropagation();
                      setSelected(node.id === selected ? null : node.id);
                    }}
                    onDoubleClick={() => node.path !== "." && setPrefix(node.path)}
                    tabIndex={0}
                    role="button"
                    aria-label={`${node.path}, ${plural(node.file_count ?? 0, "file")}`}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") setSelected(node.id);
                    }}
                  >
                    <rect width={NODE_WIDTH} height={NODE_HEIGHT} rx={10} />
                    <text x={12} y={18} className="node-label">
                      {truncate(moduleLabel(node, prefix), 22)}
                    </text>
                    <text x={12} y={32} className="node-meta">
                      {truncate(node.path, 20)} · {node.file_count ?? 0}
                    </text>
                  </g>
                );
              })}
            </svg>
          </div>
        )}

        {data && (
          <p className="graph-footnote">
            <span className="mono">
              {plural(data.nodes.length, "module")} · {plural(data.edges.length, "link")} · commit{" "}
              {data.commit_sha.slice(0, 7)}
            </span>
            <span>
              Arrows point from code to what it imports, so foundations sit{" "}
              {layout?.vertical ? "at the bottom" : "on the right"}. Click a module for its links;
              double-click to open it.
            </span>
          </p>
        )}
      </div>

      {data && selectedNode && (
        <ModuleDetail
          node={selectedNode}
          edges={data.edges}
          nodesById={nodesById}
          snapshotId={data.snapshot_id}
          onOpen={() => selectedNode.path !== "." && setPrefix(selectedNode.path)}
          onSelect={setSelected}
          onOpenSource={onOpenSource}
        />
      )}
    </>
  );
}

type Point = { x: number; y: number };

function edgePath(from: Point, to: Point, vertical: boolean): string {
  if (vertical) {
    const x1 = from.x + NODE_WIDTH / 2;
    const x2 = to.x + NODE_WIDTH / 2;
    if (to.y > from.y) {
      const y1 = from.y + NODE_HEIGHT;
      const y2 = to.y - 2;
      const bend = (y2 - y1) / 2;
      return `M${x1},${y1} C${x1},${y1 + bend} ${x2},${y2 - bend} ${x2},${y2}`;
    }
    // Same layer or pointing back (a cycle): loop below.
    const y1 = from.y + NODE_HEIGHT;
    const y2 = to.y + NODE_HEIGHT + 2;
    const reach = Math.max(y1, y2) + 30 + Math.min(40, Math.abs(x2 - x1) / 6);
    return `M${x1},${y1} C${x1},${reach} ${x2},${reach} ${x2},${y2}`;
  }
  const midY = NODE_HEIGHT / 2;
  if (to.x > from.x) {
    const x1 = from.x + NODE_WIDTH;
    const y1 = from.y + midY;
    const x2 = to.x - 2;
    const y2 = to.y + midY;
    const bend = (x2 - x1) / 2;
    return `M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`;
  }
  // Same column or pointing back (a cycle): loop around the right-hand side.
  const x1 = from.x + NODE_WIDTH;
  const y1 = from.y + midY;
  const x2 = to.x + NODE_WIDTH + 2;
  const y2 = to.y + midY;
  const reach = Math.max(x1, x2) + 40 + Math.min(60, Math.abs(y2 - y1) / 4);
  return `M${x1},${y1} C${reach},${y1} ${reach},${y2} ${x2},${y2}`;
}

function moduleLabel(node: GraphNode, prefix: string | null): string {
  if (node.path === ".") return "(root files)";
  if (prefix && node.path === prefix) return "(files here)";
  return node.label;
}

function truncate(text: string, length: number): string {
  return text.length > length ? `${text.slice(0, length - 1)}…` : text;
}

function ModuleDetail({
  node,
  edges,
  nodesById,
  snapshotId,
  onOpen,
  onSelect,
  onOpenSource,
}: {
  node: GraphNode;
  edges: GraphEdge[];
  nodesById: Map<string, GraphNode>;
  snapshotId: string;
  onOpen: () => void;
  onSelect: (id: string) => void;
  onOpenSource: (target: SourceTarget) => void;
}) {
  const uses = edges
    .filter((edge) => edge.source === node.id)
    .sort((a, b) => b.count - a.count);
  const usedBy = edges
    .filter((edge) => edge.target === node.id)
    .sort((a, b) => b.count - a.count);

  const row = (edge: GraphEdge, other: string) => (
    <li key={edge.id}>
      <button type="button" className="link-row" onClick={() => onSelect(other)}>
        <span className="mono">{nodesById.get(other)?.path ?? other}</span>
        <small>{plural(edge.count, "import")}</small>
      </button>
      <button
        type="button"
        className="citation subtle"
        title="Open the first import"
        onClick={() =>
          onOpenSource({
            path: edge.path,
            startLine: edge.line,
            endLine: edge.line,
            label: "first import",
            snapshotId,
          })
        }
      >
        <FileCode2 size={11} />
        {edge.path.split("/").pop()}:{edge.line}
      </button>
    </li>
  );

  return (
    <article className="panel module-detail">
      <div className="panel-heading">
        <div>
          <h3 className="mono">{node.path}</h3>
          <p>{plural(node.file_count ?? 0, "source file")}</p>
        </div>
        {node.path !== "." && (
          <button className="ghost-button" type="button" onClick={onOpen}>
            Open
            <ArrowRight size={13} />
          </button>
        )}
      </div>

      <div className="detail-columns">
        <div>
          <h4>Depends on ({uses.length})</h4>
          {uses.length ? (
            <ul className="link-list">{uses.map((edge) => row(edge, edge.target))}</ul>
          ) : (
            <p className="muted-line">Nothing else in view.</p>
          )}
        </div>
        <div>
          <h4>Used by ({usedBy.length})</h4>
          {usedBy.length ? (
            <ul className="link-list">{usedBy.map((edge) => row(edge, edge.source))}</ul>
          ) : (
            <p className="muted-line">Nothing else in view.</p>
          )}
        </div>
      </div>
    </article>
  );
}

/* ---------- Symbols ---------- */

function SymbolExplorer({
  repositoryId,
  onGraphInfo,
  onOpenSource,
}: {
  repositoryId: string;
  onGraphInfo: (info: GraphInfo) => void;
  onOpenSource: (target: SourceTarget) => void;
}) {
  const [queryText, setQueryText] = useState("");
  const [results, setResults] = useState<CodeSymbol[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trail, setTrail] = useState<string[]>([]);
  const [detail, setDetail] = useState<SymbolDetail | null>(null);
  const [loadingDetail, setLoadingDetail] = useState(false);
  const [hideGuesses, setHideGuesses] = useState(true);

  const current = trail.length ? trail[trail.length - 1] : null;

  useEffect(() => {
    if (!current) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    setLoadingDetail(true);
    setError(null);
    api
      .symbolDetail(repositoryId, current, hideGuesses ? GUESS_THRESHOLD : 0)
      .then((result) => {
        if (cancelled) return;
        setDetail(result);
        onGraphInfo(result.graph);
      })
      .catch((err) => !cancelled && setError(errorMessage(err)))
      .finally(() => !cancelled && setLoadingDetail(false));
    return () => {
      cancelled = true;
    };
  }, [repositoryId, current, hideGuesses]);

  async function handleSearch(event: FormEvent) {
    event.preventDefault();
    const text = queryText.trim();
    if (!text) return;
    setSearching(true);
    setError(null);
    try {
      const result = await api.searchSymbols(repositoryId, text);
      setResults(result.items);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSearching(false);
    }
  }

  function openSymbol(id: string) {
    setTrail((path) => (path[path.length - 1] === id ? path : [...path, id].slice(-20)));
  }

  return (
    <>
      <form className="panel search-form" onSubmit={handleSearch}>
        <Search size={16} />
        <input
          type="search"
          placeholder="A function, class or method name"
          value={queryText}
          onChange={(event) => setQueryText(event.target.value)}
        />
        <button
          className="primary-button"
          type="submit"
          disabled={searching || !queryText.trim()}
        >
          {searching ? <Loader2 size={15} className="spin" /> : <Search size={15} />}
          Find
        </button>
      </form>

      {error && (
        <div className="system-note error-note">
          <AlertTriangle size={17} />
          <span>{error}</span>
        </div>
      )}

      {results && !detail && !loadingDetail && (
        <div className="panel symbol-results">
          {results.length === 0 ? (
            <p className="muted-line">No symbols with that name.</p>
          ) : (
            <ul className="link-list">
              {results.map((symbol) => (
                <li key={symbol.id}>
                  <button type="button" className="link-row" onClick={() => openSymbol(symbol.id)}>
                    <span>
                      <span className="kind-tag">{symbol.kind}</span>
                      {symbol.qualified_name}
                    </span>
                    <small className="mono">
                      {symbol.path}:{symbol.start_line}
                    </small>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {loadingDetail && !detail && (
        <p className="muted-line">
          <Loader2 size={13} className="spin" /> Loading symbol…
        </p>
      )}

      {detail && (
        <SymbolView
          detail={detail}
          canGoBack={trail.length > 0}
          hideGuesses={hideGuesses}
          onToggleGuesses={() => setHideGuesses((value) => !value)}
          onBack={() => setTrail((path) => path.slice(0, -1))}
          onOpenSymbol={openSymbol}
          onOpenSource={onOpenSource}
        />
      )}
    </>
  );
}

function SymbolView({
  detail,
  canGoBack,
  hideGuesses,
  onToggleGuesses,
  onBack,
  onOpenSymbol,
  onOpenSource,
}: {
  detail: SymbolDetail;
  canGoBack: boolean;
  hideGuesses: boolean;
  onToggleGuesses: () => void;
  onBack: () => void;
  onOpenSymbol: (id: string) => void;
  onOpenSource: (target: SourceTarget) => void;
}) {
  const { symbol } = detail;
  const nodes = useMemo(
    () => new Map(detail.nodes.map((node) => [node.id, node])),
    [detail],
  );

  const callSite = (edge: GraphEdge, label: string) =>
    onOpenSource({
      path: edge.path,
      startLine: edge.line,
      endLine: edge.line,
      label,
      snapshotId: detail.snapshot_id,
    });

  const sentence = (edge: GraphEdge) =>
    `${nodes.get(edge.source)?.label ?? "?"} ${edge.relation} ${nodes.get(edge.target)?.label ?? "?"}`;

  const edgeRow = (edge: GraphEdge, otherId: string) => {
    const other = nodes.get(otherId);
    const otherSymbol = symbolIdOf(otherId);
    return (
      <li key={edge.id}>
        <button
          type="button"
          className="link-row"
          disabled={!otherSymbol}
          title={other?.qualified_name ?? undefined}
          onClick={() => otherSymbol && onOpenSymbol(otherSymbol)}
        >
          <span>
            {edge.relation !== "calls" && <span className="kind-tag">{edge.relation}</span>}
            {other?.qualified_name ?? other?.label ?? otherId}
          </span>
          <small className={`confidence ${edge.resolution}`}>
            {edge.resolution} {edge.confidence.toFixed(2)}
          </small>
        </button>
        <button
          type="button"
          className="citation subtle"
          title="Open the line"
          onClick={() => callSite(edge, sentence(edge))}
        >
          <FileCode2 size={11} />
          {edge.path.split("/").pop()}:{edge.line}
        </button>
      </li>
    );
  };

  return (
    <article className="panel symbol-view">
      <div className="panel-heading">
        <div className="source-title">
          <p className="eyebrow">{symbol.kind}</p>
          <h3 className="mono">{symbol.qualified_name}</h3>
          <p className="mono">
            {symbol.path}:{symbol.start_line}–{symbol.end_line}
          </p>
        </div>
        <div className="symbol-actions">
          {canGoBack && (
            <button className="ghost-button" type="button" onClick={onBack}>
              <ArrowLeft size={13} />
              Back
            </button>
          )}
          <button
            className="ghost-button"
            type="button"
            onClick={() =>
              onOpenSource({
                path: symbol.path,
                startLine: symbol.start_line,
                endLine: symbol.end_line,
                label: symbol.name,
                snapshotId: detail.snapshot_id,
              })
            }
          >
            <FileCode2 size={13} />
            Source
          </button>
        </div>
      </div>

      <FocusGraph detail={detail} nodes={nodes} onOpenSymbol={onOpenSymbol} />

      <div className="symbol-options">
        <label className="toggle-line">
          <input type="checkbox" checked={hideGuesses} onChange={onToggleGuesses} />
          Hide name-only guesses (confidence below {GUESS_THRESHOLD})
        </label>
        <span className="legend">
          <span className="legend-line" /> static
          <span className="legend-line inferred" /> inferred from names
        </span>
      </div>

      {detail.parent && (
        <p className="muted-line">
          Inside{" "}
          <button type="button" className="inline-link" onClick={() => onOpenSymbol(detail.parent!.id)}>
            {detail.parent.qualified_name}
          </button>
        </p>
      )}

      <div className="detail-columns">
        <div>
          <h4>Called by ({detail.incoming.length})</h4>
          {detail.incoming.length ? (
            <ul className="link-list">
              {detail.incoming.map((edge) => edgeRow(edge, edge.source))}
            </ul>
          ) : (
            <p className="muted-line">No known callers.</p>
          )}
        </div>
        <div>
          <h4>Calls ({detail.outgoing.length})</h4>
          {detail.outgoing.length ? (
            <ul className="link-list">
              {detail.outgoing.map((edge) => edgeRow(edge, edge.target))}
            </ul>
          ) : (
            <p className="muted-line">No calls into this repository.</p>
          )}
        </div>
      </div>

      {detail.children.length > 0 && (
        <div className="answer-section">
          <h4>Members ({detail.children.length})</h4>
          <div className="evidence-chips">
            {detail.children.map((child) => (
              <button
                key={child.id}
                type="button"
                className="citation subtle"
                onClick={() => onOpenSymbol(child.id)}
              >
                {child.name}
              </button>
            ))}
          </div>
        </div>
      )}

      {detail.truncated && (
        <p className="muted-line">Showing the first 200 links in each direction.</p>
      )}
    </article>
  );
}

// Callers on the left, the symbol in the middle, what it calls on the right.
function FocusGraph({
  detail,
  nodes,
  onOpenSymbol,
}: {
  detail: SymbolDetail;
  nodes: Map<string, GraphNode>;
  onOpenSymbol: (id: string) => void;
}) {
  const left = uniqueBy(detail.incoming, (edge) => edge.source).slice(0, FOCUS_SIDE_LIMIT);
  const right = uniqueBy(detail.outgoing, (edge) => edge.target).slice(0, FOCUS_SIDE_LIMIT);
  if (!left.length && !right.length) return null;

  const width = 640;
  const boxWidth = 168;
  const boxHeight = 28;
  const gap = 8;
  const rows = Math.max(left.length, right.length, 1);
  const height = rows * boxHeight + (rows - 1) * gap + 24;
  const columnY = (count: number, index: number) =>
    (height - (count * boxHeight + (count - 1) * gap)) / 2 + index * (boxHeight + gap);
  const centerX = (width - boxWidth) / 2;
  const centerY = (height - boxHeight) / 2;

  const box = (
    key: string,
    x: number,
    y: number,
    label: string,
    symbolId: string | null,
    className: string,
  ) => (
    <g
      key={key}
      className={`graph-node ${className}`}
      transform={`translate(${x}, ${y})`}
      onClick={() => symbolId && onOpenSymbol(symbolId)}
      role={symbolId ? "button" : undefined}
    >
      <rect width={boxWidth} height={boxHeight} rx={8} />
      <text x={10} y={18} className="node-label small">
        {truncate(label, 24)}
      </text>
      <title>{label}</title>
    </g>
  );

  const side = (edges: GraphEdge[], end: "source" | "target", x: number) =>
    edges.map((edge, index) => {
      const id = edge[end];
      const node = nodes.get(id);
      const y = columnY(edges.length, index);
      const fromLeft = end === "source";
      const x1 = fromLeft ? x + boxWidth : centerX + boxWidth;
      const y1 = fromLeft ? y + boxHeight / 2 : centerY + boxHeight / 2;
      const x2 = fromLeft ? centerX - 2 : x - 2;
      const y2 = fromLeft ? centerY + boxHeight / 2 : y + boxHeight / 2;
      const bend = (x2 - x1) / 2;
      return (
        <g key={edge.id}>
          <path
            className={`graph-edge ${edge.resolution === "inferred" ? "inferred" : ""}`}
            d={`M${x1},${y1} C${x1 + bend},${y1} ${x2 - bend},${y2} ${x2},${y2}`}
            markerEnd="url(#focus-arrow)"
          />
          {box(id, x, y, node?.label ?? id, symbolIdOf(id), "")}
        </g>
      );
    });

  return (
    <div className="graph-canvas focus-canvas">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        style={{ width: "100%", maxWidth: width }}
        role="img"
        aria-label="Callers and calls"
      >
        <defs>
          <marker
            id="focus-arrow"
            viewBox="0 0 8 8"
            refX="7"
            refY="4"
            markerWidth="6"
            markerHeight="6"
            orient="auto-start-reverse"
          >
            <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
          </marker>
        </defs>
        {side(left, "source", 0)}
        {side(right, "target", width - boxWidth)}
        {box("center", centerX, centerY, detail.symbol.name, null, "selected")}
      </svg>
    </div>
  );
}

function uniqueBy<T>(items: T[], key: (item: T) => string): T[] {
  const seen = new Set<string>();
  return items.filter((item) => {
    const value = key(item);
    if (seen.has(value)) return false;
    seen.add(value);
    return true;
  });
}
