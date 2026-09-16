/* eslint-disable no-await-in-loop */
// EP-2-22 console slice: knowledge-base management page (list / ingest /
// upload / delete / retrieval try-out) over the /api/knowledge endpoints.
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  Alert,
  App as AntApp,
  Button,
  Divider,
  Input,
  Popconfirm,
  Space,
  Spin,
  Tabs,
  Tag,
  Typography,
  Upload,
} from "antd";
import { InboxOutlined } from "@ant-design/icons";
import {
  knowledgeApi,
  type KnowledgeDocument,
  type KnowledgeSearchPayload,
} from "../../api/modules/knowledge";
import styles from "./index.module.less";

const { TextArea } = Input;
const { Text } = Typography;

interface TextFormValues {
  title: string;
  text: string;
  source: string;
  tags: string;
}

const EMPTY_TEXT: TextFormValues = {
  title: "",
  text: "",
  source: "",
  tags: "",
};

export default function KnowledgePage() {
  const { t } = useTranslation();
  const { message } = AntApp.useApp();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string>("");
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [form, setForm] = useState<TextFormValues>(EMPTY_TEXT);
  const [uploadTags, setUploadTags] = useState("");
  const [query, setQuery] = useState("");
  const [topK, setTopK] = useState(5);
  const [searching, setSearching] = useState(false);
  const [searchResult, setSearchResult] =
    useState<KnowledgeSearchPayload | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const payload = await knowledgeApi.list();
      setDocuments(payload.documents || []);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function submitText() {
    if (!form.title.trim() || !form.text.trim()) {
      void message.warning(t("knowledge.ingest.missing", "标题与正文都必填"));
      return;
    }
    try {
      await knowledgeApi.add(
        form.title.trim(),
        form.text,
        form.source.trim(),
        form.tags,
      );
      void message.success(t("knowledge.ingest.done", "已入库"));
      setForm(EMPTY_TEXT);
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function submitUpload(file: File) {
    try {
      await knowledgeApi.upload(file, "", uploadTags);
      void message.success(t("knowledge.ingest.done", "已入库"));
      setUploadTags("");
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
    return false;
  }

  async function removeDocument(docId: string, title: string) {
    try {
      await knowledgeApi.remove(docId);
      void message.success(
        t("knowledge.doc.deleted", { title, defaultValue: "已删除 {{title}}" }),
      );
      await load();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    }
  }

  async function runSearch() {
    if (!query.trim()) return;
    setSearching(true);
    setError("");
    try {
      setSearchResult(await knowledgeApi.search(query.trim(), topK));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setSearching(false);
    }
  }

  return (
    <div className={styles.page}>
      <div className={styles.header}>
        <h1>{t("knowledge.title", "知识库")}</h1>
        <Button onClick={() => void load()} loading={loading}>
          {t("knowledge.refresh", "刷新")}
        </Button>
      </div>
      {error ? <Alert type="error" showIcon message={error} /> : null}

      <Divider orientation="left" plain>
        {t("knowledge.ingest.title", "添加文档")}
      </Divider>
      <Tabs
        items={[
          {
            key: "text",
            label: t("knowledge.ingest.paste", "粘贴文本"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size={8}>
                <Input
                  placeholder={t("knowledge.ingest.titlePh", "标题")}
                  value={form.title}
                  onChange={(e) => setForm({ ...form, title: e.target.value })}
                />
                <TextArea
                  rows={6}
                  placeholder={t("knowledge.ingest.textPh", "正文内容")}
                  value={form.text}
                  onChange={(e) => setForm({ ...form, text: e.target.value })}
                />
                <Input
                  placeholder={t("knowledge.ingest.sourcePh", "来源（可选）")}
                  value={form.source}
                  onChange={(e) => setForm({ ...form, source: e.target.value })}
                />
                <Input
                  placeholder={t(
                    "knowledge.ingest.tagsPh",
                    "标签，逗号分隔（可选）",
                  )}
                  value={form.tags}
                  onChange={(e) => setForm({ ...form, tags: e.target.value })}
                />
                <Button type="primary" onClick={() => void submitText()}>
                  {t("knowledge.ingest.submit", "入库")}
                </Button>
              </Space>
            ),
          },
          {
            key: "upload",
            label: t("knowledge.ingest.upload", "上传文件"),
            children: (
              <Space direction="vertical" style={{ width: "100%" }} size={8}>
                <Upload.Dragger
                  accept=".txt,.md,.markdown,.json,.csv,.log"
                  showUploadList={false}
                  customRequest={async ({ file }) => {
                    await submitUpload(file as File);
                  }}
                >
                  <p className="ant-upload-drag-icon">
                    <InboxOutlined />
                  </p>
                  <p className="ant-upload-text">
                    {t(
                      "knowledge.ingest.dropHint",
                      "点击或拖拽 UTF-8 文本文件",
                    )}
                  </p>
                </Upload.Dragger>
                <Input
                  placeholder={t(
                    "knowledge.ingest.tagsPh",
                    "标签，逗号分隔（可选）",
                  )}
                  value={uploadTags}
                  onChange={(e) => setUploadTags(e.target.value)}
                />
              </Space>
            ),
          },
        ]}
      />

      <Divider orientation="left" plain>
        {t("knowledge.doc.title", "文档列表")}
      </Divider>
      {loading ? (
        <Spin />
      ) : documents.length === 0 ? (
        <Text type="secondary">{t("knowledge.doc.empty", "暂无文档")}</Text>
      ) : (
        <div className={styles.documents}>
          {documents.map((doc) => (
            <div key={doc.doc_id} className={styles.docCard}>
              <div className={styles.docMain}>
                <div className={styles.docTitle}>{doc.title}</div>
                <div className={styles.docMeta}>
                  {doc.source || "—"} ·{" "}
                  {t("knowledge.doc.chunks", {
                    count: doc.chunk_count,
                    defaultValue: "{{count}} 分块",
                  })}
                  {doc.updated_at ? ` · ${doc.updated_at}` : ""}
                </div>
              </div>
              <Space size={4} wrap>
                {doc.tags?.map((tag) => <Tag key={tag}>{tag}</Tag>)}
              </Space>
              <Popconfirm
                title={t("knowledge.doc.confirm", "删除该文档？")}
                onConfirm={() => void removeDocument(doc.doc_id, doc.title)}
              >
                <Button danger size="small">
                  {t("knowledge.doc.delete", "删除")}
                </Button>
              </Popconfirm>
            </div>
          ))}
        </div>
      )}

      <Divider orientation="left" plain>
        {t("knowledge.search.title", "检索试跑")}
      </Divider>
      <Space.Compact style={{ width: "100%", maxWidth: 640 }}>
        <Input
          placeholder={t("knowledge.search.ph", "查询语句")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onPressEnter={() => void runSearch()}
        />
        <Input
          style={{ width: 80 }}
          type="number"
          min={1}
          max={20}
          value={topK}
          onChange={(e) => setTopK(Number(e.target.value) || 5)}
        />
        <Button
          type="primary"
          loading={searching}
          onClick={() => void runSearch()}
        >
          {t("knowledge.search.run", "检索")}
        </Button>
      </Space.Compact>
      {searchResult ? (
        <div style={{ marginTop: 12 }}>
          <Tag color="blue">{searchResult.embedding_mode}</Tag>
          {searchResult.results.length === 0 ? (
            <Text type="secondary">
              {t("knowledge.search.empty", "无命中")}
            </Text>
          ) : (
            searchResult.results.map((item, index) => (
              <div
                key={`${item.doc_id}-${item.chunk_index}-${index}`}
                className={styles.searchResult}
              >
                <div className={styles.resultHead}>
                  <Text strong ellipsis style={{ maxWidth: 320 }}>
                    {item.title}
                  </Text>
                  <Tag>#{item.chunk_index}</Tag>
                  <Tag color="green">{item.score.toFixed(4)}</Tag>
                </div>
                <div className={styles.resultText}>{item.text}</div>
              </div>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}
