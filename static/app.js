// Zero-build React app (uses React UMD globals + React.createElement).
// The dashboard is embedded with the @databricks/aibi-client JavaScript SDK
// (NOT an iframe), using the signed-in user's OBO token.
const { useState, useEffect, useRef } = React;
const h = React.createElement;

const AIBI_CDN =
  "https://cdn.jsdelivr.net/npm/@databricks/aibi-client@0.0.0-alpha.7/+esm";

function App() {
  const containerRef = useRef(null);
  const [cfg, setCfg] = useState(null);
  const [embedStatus, setEmbedStatus] = useState("Loading configuration…");
  const [embedError, setEmbedError] = useState(null);

  useEffect(() => {
    fetch("/api/config")
      .then((r) => r.json())
      .then(setCfg)
      .catch((e) => setEmbedError("Could not load config: " + e));
  }, []);

  useEffect(() => {
    if (!cfg || !containerRef.current) return;
    let dashboard;
    let cancelled = false;
    (async () => {
      try {
        setEmbedStatus("Initializing embedded dashboard as you (OBO)…");
        const mod = await import(AIBI_CDN);
        if (cancelled) return;
        const DatabricksDashboard = mod.DatabricksDashboard;
        // SESSION mode (no token): the @databricks/aibi-client SDK manages the
        // embed and loads it under the viewer's OWN Databricks identity. Queries
        // run as the user, so UC row-level security on current_user() is enforced
        // (true OBO) and NO service principal is involved. This is the only
        // configuration that keeps data access on-behalf-of the user — a
        // token-based embed would require an SP, which is not OBO.
        dashboard = new DatabricksDashboard({
          instanceUrl: cfg.instanceUrl,
          workspaceId: cfg.workspaceId,
          dashboardId: cfg.dashboardId,
          token: undefined,
          container: containerRef.current,
          config: { version: 1 },
        });
        dashboard.initialize();
        setEmbedStatus("");
      } catch (e) {
        setEmbedError((e && e.message) || String(e));
      }
    })();
    return () => {
      cancelled = true;
      try {
        dashboard && dashboard.destroy && dashboard.destroy();
      } catch (e) {}
    };
  }, [cfg]);

  return h(
    "div",
    { className: "app" },
    h(
      "header",
      { className: "topbar" },
      h(
        "div",
        { className: "brand" },
        h("span", { className: "logo" }, "◆"),
        " Acme Sales Portal"
      ),
      h(
        "div",
        { className: "session" },
        cfg
          ? [
              h(
                "span",
                {
                  key: "p",
                  className: "pill " + (cfg.oboEnabled ? "ok" : "warn"),
                },
                cfg.oboEnabled ? "OBO active" : "OBO missing"
              ),
              h(
                "span",
                { key: "u", className: "user" },
                cfg.user || "signed-in user"
              ),
            ]
          : h("span", { className: "user" }, "connecting…")
      )
    ),
    h(
      "div",
      { className: "subbar" },
      "Embedded via the ",
      h("code", null, "@databricks/aibi-client"),
      " JavaScript SDK (no iframe). Data is filtered by Unity Catalog row-level security on ",
      h("code", null, "current_user()"),
      " — you only see your own orders."
    ),
    h(
      "div",
      { className: "body" },
      h(
        "main",
        { className: "dashboard-pane" },
        embedError
          ? h("div", { className: "error" }, "⚠ " + embedError)
          : embedStatus
          ? h("div", { className: "status" }, embedStatus)
          : null,
        h("div", { ref: containerRef, className: "dashboard-container" })
      ),
      h(GeniePanel, { ready: !!(cfg && cfg.oboEnabled) })
    )
  );
}

function GeniePanel({ ready }) {
  const [messages, setMessages] = useState([
    {
      role: "genie",
      text:
        "Hi! Ask me about your sales orders in natural language. I run as you, so I only see your data.",
    },
  ]);
  const [input, setInput] = useState("");
  const [convId, setConvId] = useState(null);
  const [busy, setBusy] = useState(false);
  const scrollRef = useRef(null);

  useEffect(() => {
    if (scrollRef.current)
      scrollRef.current.scrollTo(0, scrollRef.current.scrollHeight);
  }, [messages, busy]);

  async function ask(q) {
    const question = (q != null ? q : input).trim();
    if (!question || busy) return;
    setInput("");
    setMessages((m) => m.concat([{ role: "user", text: question }]));
    setBusy(true);
    try {
      const resp = await fetch("/api/genie/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, conversation_id: convId }),
      });
      const data = await resp.json();
      if (!resp.ok) throw new Error(data.error || "Genie request failed");
      if (data.conversation_id) setConvId(data.conversation_id);
      setMessages((m) =>
        m.concat([
          {
            role: "genie",
            text: data.text || "(no text answer)",
            table: data.table,
          },
        ])
      );
    } catch (e) {
      setMessages((m) => m.concat([{ role: "genie", text: "⚠ " + e.message }]));
    } finally {
      setBusy(false);
    }
  }

  const suggestions = [
    "What is my total sales amount?",
    "Show revenue by region",
    "Which product sold the most units?",
  ];

  return h(
    "aside",
    { className: "genie-pane" },
    h(
      "div",
      { className: "genie-head" },
      h("span", { className: "genie-spark" }, "✨"),
      " Ask Genie",
      h("span", { className: "genie-sub" }, "native · runs as you (OBO)")
    ),
    h(
      "div",
      { className: "genie-msgs", ref: scrollRef },
      messages.map((m, i) =>
        h(
          "div",
          { key: i, className: "msg " + m.role },
          h(
            "div",
            { className: "bubble" },
            m.text,
            m.table ? h(ResultTable, { table: m.table }) : null
          )
        )
      ),
      busy
        ? h(
            "div",
            { className: "msg genie" },
            h("div", { className: "bubble typing" }, "Genie is thinking…")
          )
        : null
    ),
    h(
      "div",
      { className: "suggestions" },
      suggestions.map((s) =>
        h(
          "button",
          { key: s, disabled: !ready || busy, onClick: () => ask(s) },
          s
        )
      )
    ),
    h(
      "form",
      {
        className: "genie-input",
        onSubmit: (e) => {
          e.preventDefault();
          ask();
        },
      },
      h("input", {
        placeholder: ready ? "Ask about your sales…" : "OBO not active",
        value: input,
        disabled: !ready || busy,
        onChange: (e) => setInput(e.target.value),
      }),
      h(
        "button",
        { type: "submit", disabled: !ready || busy },
        "Send"
      )
    )
  );
}

function ResultTable({ table }) {
  if (!table || !table.columns || !table.columns.length) return null;
  return h(
    "div",
    { className: "restable" },
    h(
      "table",
      null,
      h(
        "thead",
        null,
        h(
          "tr",
          null,
          table.columns.map((c) => h("th", { key: c }, c))
        )
      ),
      h(
        "tbody",
        null,
        table.rows.slice(0, 15).map((row, i) =>
          h(
            "tr",
            { key: i },
            row.map((cell, j) => h("td", { key: j }, String(cell)))
          )
        )
      )
    )
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(h(App));
