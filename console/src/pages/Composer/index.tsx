/* eslint-disable no-await-in-loop */
// EP-2-18: DAG canvas composer over the graph orchestration API.
// Deliberately dependency-free canvas (absolute-positioned nodes +
// SVG bezier edges) — reactflow stays out of the bundle until the
// canvas grows richer.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Alert,
  App as AntApp,
  Button,
  Divider,
  Input,
  message,
  Space,
  Tag,
} from "antd";
import {
  graphApi,
  type GraphEdgeSpec,
  type GraphNodeSpec,
  type GraphRunPayload,
  type GraphSpec,
} from "../../api/modules/graph";
import styles from "./index.module.less";

interface CanvasNode extends GraphNodeSpec {
  x: number;
  y: number;
}

const NODE_WIDTH = 150;
const NODE_HEIGHT = 48;

function uid(kind: string) {
  return `${kind}-${Math.random().toString(36).slice(2, 7)}`;
}

export default function ComposerPage() {
  const { t } = useTranslation();
  const [nodes, setNodes] = useState<CanvasNode[]>([]);
  const [edges, setEdges] = useState<GraphEdgeSpec[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [linkFrom, setLinkFrom] = useState<string | null>(null);
  const [linkWhen, setLinkWhen] = useState<string>("default");
  const [graphId, setGraphId] = useState("my-flow");
  const [graphName, setGraphName] = useState("My flow");
  const [templates, setTemplates] = useState<GraphSpec[]>([]);
  const [run, setRun] = useState<GraphRunPayload | null>(null);
  const [busy, setBusy] = useState(false);
  const dragRef = useRef<{
    id: string;
    dx: number;
    dy: number;
  } | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);

  const refreshTemplates = useCallback(async () => {
    try {
      const response = await graphApi.templates();
      setTemplates(response.templates || []);
    } catch {
      // list failures surface as an empty hall
      setTemplates([]);
    }
  }, []);

  useEffect(() => {
    void refreshTemplates();
  }, [refreshTemplates]);

  const selectedNode = useMemo(
    () => nodes.find((node) => node.id === selected) || null,
    [nodes, selected],
  );

  // ------------------------------------------------------------ editing

  const addNode = (kind: "agent" | "gate") => {
    const id = uid(kind);
    setNodes((current) => [
      ...current,
      {
        id,
        kind,
        title: kind === "gate" ? "Human Gate" : "Agent",
        params:
          kind === "gate"
            ? {
                message: t(
                  "composer.defaultGateMessage",
                  "Approve to continue?",
                ),
              }
            : { prompt: "" },
        x: 80 + Math.random() * 220,
        y: 80 + Math.random() * 180,
      },
    ]);
    setSelected(id);
  };

  const updateSelected = (patch: Partial<CanvasNode>) => {
    if (!selectedNode) return;
    setNodes((current) =>
      current.map((node) =>
        node.id === selectedNode.id ? { ...node, ...patch } : node,
      ),
    );
  };

  const updateParam = (key: string, value: string) => {
    if (!selectedNode) return;
    updateSelected({
      params: { ...selectedNode.params, [key]: value },
    });
  };

  const deleteSelected = () => {
    if (!selectedNode) return;
    setNodes((current) =>
      current.filter((node) => node.id !== selectedNode.id),
    );
    setEdges((current) =>
      current.filter(
        (edge) => edge.from !== selectedNode.id && edge.to !== selectedNode.id,
      ),
    );
    setSelected(null);
  };

  const startLink = (nodeId: string) => {
    setLinkFrom(nodeId);
    const isGate = nodes.find((node) => node.id === nodeId)?.kind === "gate";
    setLinkWhen(isGate ? "approve" : "default");
  };

  const completeLink = (nodeId: string) => {
    if (linkFrom && linkFrom !== nodeId) {
      setEdges((current) => [
        ...current.filter(
          (edge) =>
            !(edge.from === linkFrom && (edge.when || "default") === linkWhen),
        ),
        { from: linkFrom, to: nodeId, when: linkWhen },
      ]);
    }
    setLinkFrom(null);
  };

  // ------------------------------------------------------------- drag

  const onNodePointerDown = (event: React.PointerEvent, node: CanvasNode) => {
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!rect) return;
    dragRef.current = {
      id: node.id,
      dx: event.clientX - rect.left - node.x,
      dy: event.clientY - rect.top - node.y,
    };
    setSelected(node.id);
    (event.target as HTMLElement).setPointerCapture(event.pointerId);
  };

  const onCanvasPointerMove = (event: React.PointerEvent) => {
    const drag = dragRef.current;
    const rect = canvasRef.current?.getBoundingClientRect();
    if (!drag || !rect) return;
    const x = Math.max(0, event.clientX - rect.left - drag.dx);
    const y = Math.max(0, event.clientY - rect.top - drag.dy);
    setNodes((current) =>
      current.map((node) => (node.id === drag.id ? { ...node, x, y } : node)),
    );
  };

  // ------------------------------------------------------ build & run

  const buildSpec = (): GraphSpec => {
    const entries = nodes.filter(
      (node) => !edges.some((edge) => edge.to === node.id),
    );
    return {
      id: graphId.trim() || "my-flow",
      name: graphName.trim() || graphId,
      entry: entries[0]?.id || nodes[0]?.id || "",
      nodes: nodes.map(({ id, kind, title, params }) => ({
        id,
        kind,
        title,
        params,
      })),
      edges,
    };
  };

  const publish = async () => {
    setBusy(true);
    try {
      await graphApi.publish(buildSpec());
      void message.success(
        t("composer.published", "Template published to the hall"),
      );
      await refreshTemplates();
    } catch (error) {
      void message.error(String(error));
    } finally {
      setBusy(false);
    }
  };

  const launch = async (templateId: string) => {
    setBusy(true);
    try {
      const started = await graphApi.start(templateId);
      setRun(started);
      if (started.status === "suspended") {
        void message.warning(
          t("composer.suspended", "Run suspended at a human gate"),
        );
      }
    } catch (error) {
      void message.error(String(error));
    } finally {
      setBusy(false);
    }
  };

  const resolve = async (route: "approve" | "deny") => {
    if (!run) return;
    setBusy(true);
    try {
      const resumed = await graphApi.resume(run.run_id, route);
      setRun(resumed);
    } catch (error) {
      void message.error(String(error));
    } finally {
      setBusy(false);
    }
  };

  // --------------------------------------------------------- edges svg

  const edgeGeometry = useMemo(() => {
    const byId = new Map(nodes.map((node) => [node.id, node]));
    return edges
      .map((edge) => {
        const from = byId.get(edge.from);
        const to = byId.get(edge.to);
        if (!from || !to) return null;
        const x1 = from.x + NODE_WIDTH;
        const y1 = from.y + NODE_HEIGHT / 2;
        const x2 = to.x;
        const y2 = to.y + NODE_HEIGHT / 2;
        const mid = (x1 + x2) / 2;
        return {
          key: `${edge.from}->${edge.to}:${edge.when || "default"}`,
          d: `M ${x1} ${y1} C ${mid} ${y1}, ${mid} ${y2}, ${x2} ${y2}`,
          label: edge.when && edge.when !== "default" ? edge.when : "",
          lx: mid,
          ly: (y1 + y2) / 2 - 4,
        };
      })
      .filter(Boolean) as {
      key: string;
      d: string;
      label: string;
      lx: number;
      ly: number;
    }[];
  }, [nodes, edges]);

  return (
    <AntApp>
      <div className={styles.layout}>
        {/* palette */}
        <div className={styles.panel}>
          <Divider orientation="left" plain>
            {t("composer.palette", "Nodes")}
          </Divider>
          <div
            className={styles.paletteItem}
            onClick={() => addNode("agent")}
            role="button"
            tabIndex={0}
            onKeyDown={() => undefined}
          >
            {t("composer.agentNode", "Agent / Prompt")}
          </div>
          <div
            className={styles.paletteItem}
            onClick={() => addNode("gate")}
            role="button"
            tabIndex={0}
            onKeyDown={() => undefined}
          >
            {t("composer.gateNode", "Human Gate")}
          </div>
          <Divider orientation="left" plain>
            {t("composer.graphMeta", "Graph")}
          </Divider>
          <Input
            value={graphId}
            onChange={(event) => setGraphId(event.target.value)}
            placeholder="flow-id"
            size="small"
          />
          <Input
            value={graphName}
            onChange={(event) => setGraphName(event.target.value)}
            placeholder={t("composer.namePlaceholder", "Display name")}
            size="small"
            style={{ marginTop: 8 }}
          />
          <Button
            block
            type="primary"
            style={{ marginTop: 12 }}
            loading={busy}
            disabled={!nodes.length}
            onClick={() => void publish()}
          >
            {t("composer.publish", "Publish")}
          </Button>
          {linkFrom && (
            <Alert
              style={{ marginTop: 8 }}
              type="info"
              showIcon={false}
              message={`${t(
                "composer.linking",
                "Linking from",
              )} ${linkFrom} (${linkWhen}) — ${t(
                "composer.clickTarget",
                "click target node",
              )}`}
            />
          )}
        </div>

        {/* canvas */}
        <div
          className={styles.canvas}
          ref={canvasRef}
          onPointerMove={onCanvasPointerMove}
          onPointerUp={() => {
            dragRef.current = null;
          }}
        >
          <svg className={styles.svgLayer}>
            {edgeGeometry.map((edge) => (
              <g key={edge.key}>
                <path className={styles.edgePath} d={edge.d} />
                {edge.label && (
                  <text className={styles.edgeLabel} x={edge.lx} y={edge.ly}>
                    {edge.label}
                  </text>
                )}
              </g>
            ))}
          </svg>
          {nodes.map((node) => (
            <div
              key={node.id}
              className={`${styles.node} ${
                node.kind === "gate" ? styles.gate : ""
              } ${selected === node.id ? styles.selected : ""}`}
              style={{ left: node.x, top: node.y }}
              onPointerDown={(event) => onNodePointerDown(event, node)}
              onClick={() =>
                linkFrom ? completeLink(node.id) : setSelected(node.id)
              }
            >
              <div className={styles.nodeKind}>{node.kind}</div>
              <div className={styles.nodeTitle}>{node.title || node.id}</div>
              <div
                className={styles.anchor}
                onClick={(event) => {
                  event.stopPropagation();
                  startLink(node.id);
                }}
                role="button"
                tabIndex={0}
                onKeyDown={() => undefined}
              />
            </div>
          ))}
        </div>

        {/* properties */}
        <div className={styles.panel}>
          <Divider orientation="left" plain>
            {t("composer.properties", "Properties")}
          </Divider>
          {selectedNode ? (
            <>
              <div className={styles.statusLine}>id: {selectedNode.id}</div>
              {selectedNode.kind === "gate" ? (
                <Input
                  value={String(selectedNode.params.message || "")}
                  onChange={(event) =>
                    updateParam("message", event.target.value)
                  }
                  placeholder={t(
                    "composer.gateMessage",
                    "Question on the approval card",
                  )}
                  size="small"
                  style={{ marginTop: 8 }}
                />
              ) : (
                <Input.TextArea
                  value={String(selectedNode.params.prompt || "")}
                  onChange={(event) =>
                    updateParam("prompt", event.target.value)
                  }
                  placeholder={t("composer.promptPlaceholder", "Prompt")}
                  rows={4}
                  style={{ marginTop: 8 }}
                />
              )}
              <Button
                block
                danger
                size="small"
                style={{ marginTop: 12 }}
                onClick={deleteSelected}
              >
                {t("composer.deleteNode", "Delete node")}
              </Button>
            </>
          ) : (
            <div className={styles.statusLine}>
              {t("composer.noSelection", "Select a node")}
            </div>
          )}
        </div>

        {/* hall */}
        <div className={styles.hall}>
          <Divider orientation="left" plain>
            {t("composer.hall", "Hall — published templates")}
          </Divider>
          {templates.map((template) => (
            <div key={template.id} className={styles.templateCard}>
              <div className={styles.templateName}>{template.name}</div>
              <div className={styles.templateMeta}>
                {template.id} · {template.nodes.length}{" "}
                {t("composer.nodes", "nodes")}
              </div>
              <Button
                size="small"
                type="primary"
                loading={busy}
                onClick={() => void launch(template.id)}
              >
                {t("composer.launch", "Launch")}
              </Button>
            </div>
          ))}
          {!templates.length && (
            <div className={styles.statusLine}>
              {t("composer.noTemplates", "No templates published yet")}
            </div>
          )}
          {run && (
            <>
              <Divider plain>
                {t("composer.run", "Run")} {run.run_id}
              </Divider>
              <Space wrap>
                <Tag
                  color={
                    run.status === "completed"
                      ? "green"
                      : run.status === "suspended"
                      ? "orange"
                      : run.status === "failed"
                      ? "red"
                      : "blue"
                  }
                >
                  {run.status}
                </Tag>
              </Space>
              {run.status === "suspended" && (
                <Space style={{ marginTop: 8 }}>
                  <Button
                    size="small"
                    type="primary"
                    loading={busy}
                    onClick={() => void resolve("approve")}
                  >
                    {t("composer.approve", "Approve")}
                  </Button>
                  <Button
                    size="small"
                    danger
                    loading={busy}
                    onClick={() => void resolve("deny")}
                  >
                    {t("composer.deny", "Deny")}
                  </Button>
                </Space>
              )}
              {run.nodes.map((nodeRun) => (
                <div key={nodeRun.node_id} className={styles.statusLine}>
                  {nodeRun.node_id}: {nodeRun.status}
                  {nodeRun.route ? ` (${nodeRun.route})` : ""}
                </div>
              ))}
            </>
          )}
        </div>
      </div>
    </AntApp>
  );
}
