/**
 * qa-data — Ask 页 (J4 / EP-2-6 M1).
 *
 * Host-loaded plugin module (usePluginLoader): registers a React route
 * at /apps/qa-data. React/antd come from window.QwenPaw.host — no
 * bundler. Page flow: pick table (optional) → ask → answer card with
 * the exact SQL block + result table (SQL 透明, J4).
 */
(function () {
  var QwenPaw = window.QwenPaw;
  if (!QwenPaw || !QwenPaw.host || !QwenPaw.registerRoutes) {
    console.error("[qa-data] window.QwenPaw not ready — cannot register.");
    return;
  }
  var React = QwenPaw.host.React;
  var antd = QwenPaw.host.antd;

  function extractSql(answer) {
    var m = /```sql\n([\s\S]*?)```/.exec(answer || "");
    return m ? m[1].trim() : null;
  }

  function extractTable(answer) {
    var lines = (answer || "").split("\n");
    var rows = [];
    for (var i = 0; i < lines.length; i++) {
      if (/^\s*\|.*\|\s*$/.test(lines[i]) && !/^\s*\|[\s:|-]+\|\s*$/.test(lines[i])) {
        rows.push(
          lines[i].trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(function (c) { return c.trim(); })
        );
      }
    }
    if (rows.length < 2) return null;
    return { header: rows[0], body: rows.slice(1) };
  }

  var AskPage = function () {
    var _React$useState = React.useState(""),
      question = _React$useState[0],
      setQuestion = _React$useState[1];
    var _React$useState2 = React.useState(""),
      answer = _React$useState2[0],
      setAnswer = _React$useState2[1];
    var _React$useState3 = React.useState([]),
      tables = _React$useState3[0],
      setTables = _React$useState3[1];
    var _React$useState4 = React.useState(""),
      table = _React$useState4[0],
      setTable = _React$useState4[1];
    var _React$useState5 = React.useState(false),
      loading = _React$useState5[0],
      setLoading = _React$useState5[1];
    var _React$useState6 = React.useState(null),
      error = _React$useState6[0],
      setError = _React$useState6[1];

    React.useEffect(function () {
      fetch("/api/qa-data/tables")
        .then(function (r) { return r.json(); })
        .then(function (d) { setTables(d.tables || []); })
        .catch(function () { setError("无法加载表清单"); });
    }, []);

    var ask = function () {
      if (!question.trim()) return;
      setLoading(true);
      setError(null);
      setAnswer("");
      fetch("/api/qa-data/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: question, table: table || null }),
      })
        .then(function (r) {
          if (!r.ok) throw new Error("HTTP " + r.status);
          return r.json();
        })
        .then(function (d) { setAnswer(d.answer || ""); })
        .catch(function (e) { setError(String(e)); })
        .finally(function () { setLoading(false); });
    };

    var sql = extractSql(answer);
    var result = extractTable(answer);

    return React.createElement(
      "div",
      { style: { padding: 24, maxWidth: 960, margin: "0 auto" } },
      React.createElement(
        antd.Typography.Title,
        { level: 3 },
        "📊 问数 · 内网数据问答"
      ),
      React.createElement(
        antd.Space.Compact,
        { style: { width: "100%", marginBottom: 12 } },
        React.createElement(
          antd.Select,
          {
            style: { width: 260 },
            placeholder: "限定表（可选）",
            allowClear: true,
            value: table || undefined,
            onChange: function (v) { setTable(v || ""); },
            options: tables.map(function (t) {
              return { value: t.name, label: t.comment ? t.name + " · " + t.comment : t.name };
            }),
          }
        ),
        React.createElement(antd.Input, {
          style: { flex: 1 },
          placeholder: "用自然语言提问，例如：上季度订单金额最高的前 10 个客户",
          value: question,
          onChange: function (e) { setQuestion(e.target.value); },
          onPressEnter: ask,
        }),
        React.createElement(
          antd.Button,
          { type: "primary", loading: loading, onClick: ask },
          "提问"
        )
      ),
      error &&
        React.createElement(
          antd.Alert,
          { type: "error", message: error, style: { marginBottom: 12 } }
        ),
      answer &&
        React.createElement(
          "div",
          null,
          React.createElement(
            antd.Typography.Paragraph,
            { style: { whiteSpace: "pre-wrap" } },
            answer
          ),
          sql &&
            React.createElement(
              antd.Card,
              { title: "执行的 SQL（透明展示）", size: "small", style: { marginTop: 8 } },
              React.createElement(
                "pre",
                { style: { margin: 0, background: "#f6f8fa", padding: 12, borderRadius: 6 } },
                sql
              )
            ),
          result &&
            React.createElement(
              antd.Card,
              { title: "结果表格", size: "small", style: { marginTop: 12 } },
              React.createElement(
                antd.Table,
                {
                  size: "small",
                  pagination: { pageSize: 10 },
                  dataSource: result.body.map(function (row, i) {
                    var obj = { key: i };
                    result.header.forEach(function (h, j) { obj["c" + j] = row[j]; });
                    return obj;
                  }),
                  columns: result.header.map(function (h, j) {
                    return { title: h, dataIndex: "c" + j, key: "c" + j };
                  }),
                }
              )
            )
        )
    );
  };

  QwenPaw.registerRoutes([
    {
      path: "/apps/qa-data",
      exact: true,
      component: AskPage,
      meta: { title: "问数", icon: "📊" },
    },
  ]);
})();
