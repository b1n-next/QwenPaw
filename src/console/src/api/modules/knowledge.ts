// EP-2-22 console slice: knowledge-base API client (graph.ts convention).
import { clearAuthToken, getApiToken, getApiUrl } from "../config";
import { responseErrorMessage } from "../error";

// The knowledge endpoints are Form-encoded (FastAPI Form/File), so this
// request variant ships FormData bodies without a manual Content-Type
// (the browser must set the multipart boundary itself).
async function requestForm<T>(
  path: string,
  method: "POST" | "DELETE",
  body?: FormData,
): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    method,
    headers: { Authorization: `Bearer ${getApiToken()}` },
    body,
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
  return response.json() as Promise<T>;
}

async function requestGet<T>(path: string): Promise<T> {
  const response = await fetch(getApiUrl(path), {
    headers: { Authorization: `Bearer ${getApiToken()}` },
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
  return response.json() as Promise<T>;
}

export interface KnowledgeDocument {
  doc_id: string;
  title: string;
  source: string;
  tags: string[];
  chunk_count: number;
  updated_at: string;
}

export interface KnowledgeSearchResult {
  doc_id: string;
  title: string;
  chunk_index: number;
  score: number;
  text: string;
}

export interface KnowledgeSearchPayload {
  results: KnowledgeSearchResult[];
  embedding_mode: string;
}

export const knowledgeApi = {
  list() {
    return requestGet<{ documents: KnowledgeDocument[] }>(
      "/knowledge/documents",
    );
  },
  add(title: string, text: string, source: string, tags: string) {
    const form = new FormData();
    form.set("title", title);
    form.set("text", text);
    form.set("source", source);
    form.set("tags", tags);
    return requestForm<{ document: KnowledgeDocument }>(
      "/knowledge/documents",
      "POST",
      form,
    );
  },
  upload(file: File, title: string, tags: string) {
    const form = new FormData();
    form.set("file", file);
    form.set("title", title);
    form.set("tags", tags);
    return requestForm<{ document: KnowledgeDocument }>(
      "/knowledge/documents/upload",
      "POST",
      form,
    );
  },
  remove(docId: string) {
    return requestForm<{ deleted: string }>(
      `/knowledge/documents/${encodeURIComponent(docId)}`,
      "DELETE",
    );
  },
  search(query: string, topK: number) {
    const form = new FormData();
    form.set("query", query);
    form.set("top_k", String(topK));
    return requestForm<KnowledgeSearchPayload>(
      "/knowledge/search",
      "POST",
      form,
    );
  },
};
