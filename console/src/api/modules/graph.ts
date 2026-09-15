// EP-2-18: graph orchestration API client (hub.ts request convention).
import { clearAuthToken, getApiToken, getApiUrl } from "../config";
import { responseErrorMessage } from "../error";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getApiToken();
  const response = await fetch(getApiUrl(path), {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...init?.headers,
    },
  });
  if (response.status === 401) {
    clearAuthToken();
    window.location.assign("/login");
    throw new Error("Authentication expired");
  }
  if (!response.ok) {
    throw new Error(
      await responseErrorMessage(
        response,
        `Request failed with ${response.status}`,
      ),
    );
  }
  if (response.status === 204) return undefined as T;
  return response.json();
}

export interface GraphNodeSpec {
  id: string;
  kind: string;
  title?: string;
  params: Record<string, unknown>;
}

export interface GraphEdgeSpec {
  from: string;
  to: string;
  when?: string;
}

export interface GraphSpec {
  id: string;
  name: string;
  description?: string;
  entry: string;
  nodes: GraphNodeSpec[];
  edges: GraphEdgeSpec[];
}

export interface GraphNodeRun {
  node_id: string;
  status: string;
  outputs: Record<string, unknown>;
  route: string | null;
}

export interface GraphRunPayload {
  run_id: string;
  status: "completed" | "suspended" | "failed" | "running";
  suspended_at: string | null;
  error: string | null;
  state: Record<string, unknown>;
  nodes: GraphNodeRun[];
}

export const graphApi = {
  validate(graph: GraphSpec) {
    return request<{ valid: boolean; node_count: number }>("/graph/validate", {
      method: "POST",
      body: JSON.stringify({ graph }),
    });
  },
  publish(graph: GraphSpec) {
    return request<{ template: GraphSpec }>("/graph/publish", {
      method: "POST",
      body: JSON.stringify({ graph }),
    });
  },
  templates() {
    return request<{ templates: GraphSpec[] }>("/graph/templates");
  },
  start(templateId: string, inputs: Record<string, unknown> = {}) {
    return request<GraphRunPayload>("/graph/runs", {
      method: "POST",
      body: JSON.stringify({ template_id: templateId, inputs }),
    });
  },
  getRun(runId: string) {
    return request<{
      run: {
        run_id: string;
        status: string;
        state: Record<string, unknown>;
      };
      nodes: GraphNodeRun[];
    }>(`/graph/runs/${runId}`);
  },
  resume(runId: string, route: string) {
    return request<GraphRunPayload>(`/graph/runs/${runId}/resume`, {
      method: "POST",
      body: JSON.stringify({ route }),
    });
  },
};
