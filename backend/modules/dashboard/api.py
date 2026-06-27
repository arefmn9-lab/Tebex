import json
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import HTMLResponse

from modules.dashboard.schemas import RunAIRequest, SendMessageRequest
from modules.dashboard.services import DashboardService

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("", response_class=HTMLResponse)
def dashboard_home():
    return HTMLResponse(DASHBOARD_HTML)


@router.get("/locales/{language}")
def get_locale(language: str):
    if language not in {"fa", "en"}:
        raise HTTPException(status_code=404, detail="Locale not found")

    locale_path = Path(__file__).resolve().parents[3] / "frontend" / "locales" / f"{language}.json"
    if not locale_path.exists():
        raise HTTPException(status_code=404, detail="Locale not found")

    return json.loads(locale_path.read_text(encoding="utf-8"))


@router.get("/overview")
def get_overview():
    return DashboardService.overview()


@router.get("/accounts")
def get_accounts():
    return DashboardService.accounts()


@router.get("/messages")
def get_messages():
    return DashboardService.messages()


@router.get("/logs")
def get_logs(limit: int = Query(default=100, ge=1, le=500)):
    return DashboardService.logs(limit=limit)


@router.post("/send_message")
def send_message(request: SendMessageRequest):
    return DashboardService.send_message(request)


@router.post("/run_ai")
def run_ai(request: RunAIRequest):
    return DashboardService.run_ai(request)


DASHBOARD_HTML = """
<!doctype html>
<html lang="fa" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title data-i18n="app.title">ClinicOS</title>
  <style>
    :root {
      color-scheme: light;
      font-family: Inter, Segoe UI, Arial, sans-serif;
      background: #f5f7fb;
      color: #172033;
    }
    * { box-sizing: border-box; }
    body { margin: 0; }
    html[dir="rtl"] body { direction: rtl; }
    html[dir="ltr"] body { direction: ltr; }
    header {
      padding: 18px 24px;
      border-bottom: 1px solid #d8deea;
      background: #ffffff;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .header-actions { display: flex; align-items: center; gap: 14px; }
    h1 { margin: 0; font-size: 22px; line-height: 1.2; }
    main { padding: 20px 24px 32px; max-width: 1320px; margin: 0 auto; }
    .status { display: flex; gap: 8px; align-items: center; font-size: 14px; }
    .dot { width: 10px; height: 10px; border-radius: 50%; background: #1f9d55; }
    .metrics {
      display: grid;
      grid-template-columns: repeat(6, minmax(120px, 1fr));
      gap: 12px;
      margin-bottom: 18px;
    }
    .metric, section {
      background: #ffffff;
      border: 1px solid #d8deea;
      border-radius: 8px;
    }
    .metric { padding: 14px; }
    .label { color: #5d6b83; font-size: 12px; margin-bottom: 6px; }
    .value { font-size: 24px; font-weight: 700; }
    .grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 16px;
      align-items: start;
    }
    section { padding: 16px; min-width: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { text-align: left; padding: 9px 8px; border-bottom: 1px solid #edf0f5; vertical-align: top; }
    html[dir="rtl"] th, html[dir="rtl"] td { text-align: right; }
    th { color: #5d6b83; font-weight: 600; }
    input, textarea {
      width: 100%;
      border: 1px solid #c9d1df;
      border-radius: 6px;
      padding: 9px 10px;
      font: inherit;
      background: #ffffff;
    }
    textarea { min-height: 86px; resize: vertical; }
    .form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
    .form-row { margin-bottom: 10px; }
    button {
      border: 0;
      border-radius: 6px;
      background: #2563eb;
      color: #ffffff;
      padding: 10px 14px;
      font-weight: 700;
      cursor: pointer;
    }
    button.secondary { background: #334155; }
    button.language {
      background: #e2e8f0;
      color: #172033;
      min-width: 76px;
    }
    .actions { display: flex; gap: 10px; flex-wrap: wrap; }
    pre {
      margin: 0;
      overflow: auto;
      white-space: pre-wrap;
      word-break: break-word;
      font-size: 12px;
      line-height: 1.45;
      max-height: 360px;
    }
    .pill {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: #eef2ff;
      color: #3730a3;
      font-size: 12px;
      font-weight: 700;
    }
    @media (max-width: 980px) {
      .metrics { grid-template-columns: repeat(2, 1fr); }
      .grid { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1 data-i18n="app.title"></h1>
    <div class="header-actions">
      <button id="languageToggle" class="language" type="button" onclick="toggleLanguage()"></button>
      <div class="status"><span class="dot"></span><span id="systemStatus" data-i18n="status.loading"></span></div>
    </div>
  </header>
  <main>
    <div class="metrics">
      <div class="metric"><div class="label" data-i18n="metrics.accounts"></div><div id="accountsTotal" class="value">0</div></div>
      <div class="metric"><div class="label" data-i18n="metrics.active"></div><div id="activeAccounts" class="value">0</div></div>
      <div class="metric"><div class="label" data-i18n="metrics.sentToday"></div><div id="sentToday" class="value">0</div></div>
      <div class="metric"><div class="label" data-i18n="metrics.warmup"></div><div id="warmupAccounts" class="value">0</div></div>
      <div class="metric"><div class="label" data-i18n="metrics.queued"></div><div id="queuedMessages" class="value">0</div></div>
      <div class="metric"><div class="label" data-i18n="metrics.success"></div><div id="successRate" class="value">0%</div></div>
    </div>
    <div class="grid">
      <section>
        <h2 data-i18n="sections.messageSender"></h2>
        <div class="form-grid">
          <div class="form-row"><input id="platform" data-i18n-placeholder="inputs.platform" value="telegram"></div>
          <div class="form-row"><input id="accountId" type="number" data-i18n-placeholder="inputs.accountId" value="1"></div>
        </div>
        <div class="form-row"><input id="target" data-i18n-placeholder="inputs.target"></div>
        <div class="form-row"><textarea id="message" data-i18n-placeholder="inputs.customerMessage"></textarea></div>
        <div class="actions">
          <button onclick="sendMessage()" data-i18n="buttons.sendSalesHub"></button>
          <button class="secondary" onclick="runAI()" data-i18n="buttons.triggerAI"></button>
        </div>
      </section>
      <section>
        <h2 data-i18n="sections.actionResult"></h2>
        <pre id="actionResult" data-i18n="empty.noAction"></pre>
      </section>
      <section>
        <h2 data-i18n="sections.accounts"></h2>
        <div id="accountsTable"></div>
      </section>
      <section>
        <h2 data-i18n="sections.queuedMessages"></h2>
        <div id="messagesTable"></div>
      </section>
      <section style="grid-column: 1 / -1;">
        <h2 data-i18n="sections.liveLogs"></h2>
        <pre id="logs" data-i18n="logs.loading"></pre>
      </section>
    </div>
  </main>
  <script>
    let currentLanguage = localStorage.getItem("dashboardLanguage") || "fa";
    let translations = {};

    async function loadTranslations(language) {
      const response = await fetch(`/dashboard/locales/${language}`);
      if (!response.ok) throw new Error("Locale load failed");
      translations = await response.json();
      currentLanguage = language;
      localStorage.setItem("dashboardLanguage", language);
      document.documentElement.lang = language;
      document.documentElement.dir = language === "fa" ? "rtl" : "ltr";
      applyTranslations();
    }

    function t(key) {
      return translations[key] || key;
    }

    function applyTranslations() {
      document.querySelectorAll("[data-i18n]").forEach(element => {
        element.textContent = t(element.dataset.i18n);
      });
      document.querySelectorAll("[data-i18n-placeholder]").forEach(element => {
        element.placeholder = t(element.dataset.i18nPlaceholder);
      });
      document.title = t("app.title");
      document.getElementById("languageToggle").textContent = t("language.toggle");
    }

    async function toggleLanguage() {
      await loadTranslations(currentLanguage === "fa" ? "en" : "fa");
      refresh();
    }

    async function api(path, options) {
      const response = await fetch(path, options);
      const data = await response.json();
      if (!response.ok) throw new Error(JSON.stringify(data));
      return data;
    }
    function requestBody() {
      return {
        platform: document.getElementById("platform").value,
        account_id: Number(document.getElementById("accountId").value),
        target: document.getElementById("target").value,
        message: document.getElementById("message").value
      };
    }
    async function sendMessage() {
      const result = await api("/dashboard/send_message", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(requestBody())
      });
      document.getElementById("actionResult").textContent = JSON.stringify(result, null, 2);
      refresh();
    }
    async function runAI() {
      const result = await api("/dashboard/run_ai", {
        method: "POST",
        headers: {"Content-Type": "application/json"},
        body: JSON.stringify(requestBody())
      });
      document.getElementById("actionResult").textContent = JSON.stringify(result, null, 2);
      refresh();
    }
    function renderTable(rows, columns) {
      if (!rows.length) return `<div class='label'>${t("empty.noData")}</div>`;
      return "<table><thead><tr>" + columns.map(c => `<th>${t(c.labelKey)}</th>`).join("") +
        "</tr></thead><tbody>" + rows.map(row => "<tr>" + columns.map(c => {
          const value = c.render ? c.render(row) : row[c.key];
          return `<td>${value ?? ""}</td>`;
        }).join("") + "</tr>").join("") + "</tbody></table>";
    }
    async function refresh() {
      const [overview, accounts, messages, logs] = await Promise.all([
        api("/dashboard/overview"),
        api("/dashboard/accounts"),
        api("/dashboard/messages"),
        api("/dashboard/logs")
      ]);
      document.getElementById("systemStatus").textContent = t(`status.${overview.system_status}`) || overview.system_status;
      document.getElementById("accountsTotal").textContent = overview.accounts_total;
      document.getElementById("activeAccounts").textContent = overview.active_accounts;
      document.getElementById("sentToday").textContent = overview.messages_sent_today;
      document.getElementById("warmupAccounts").textContent = overview.warmup_accounts;
      document.getElementById("queuedMessages").textContent = overview.queued_messages;
      document.getElementById("successRate").textContent = Math.round(overview.success_rate * 100) + "%";
      document.getElementById("accountsTable").innerHTML = renderTable(accounts, [
        {key:"id", labelKey:"table.id"},
        {key:"platform_icon", labelKey:"table.platform"},
        {key:"username", labelKey:"table.username"},
        {key:"login_status", labelKey:"table.login", render:r => translateLoginStatus(r.login_status)},
        {key:"status", labelKey:"table.status", render:r => `<span class='pill'>${r.status}</span>`},
        {key:"warmup_status", labelKey:"table.warmup"},
        {key:"sent_today", labelKey:"table.sent"},
        {key:"daily_limit", labelKey:"table.limit"}
      ]);
      document.getElementById("messagesTable").innerHTML = renderTable(messages, [
        {key:"account_id", labelKey:"table.account"},
        {key:"platform", labelKey:"table.platform"},
        {key:"delay_seconds", labelKey:"table.delay"},
        {key:"not_before", labelKey:"table.notBefore"}
      ]);
      document.getElementById("logs").textContent = JSON.stringify(logs.slice().reverse(), null, 2);
    }

    function translateLoginStatus(status) {
      const normalized = String(status || "").toLowerCase();
      if (normalized === "logged in") return t("login.loggedIn");
      if (normalized === "login pending") return t("login.pending");
      if (normalized === "not logged in") return t("login.notLoggedIn");
      return status || "";
    }

    loadTranslations(currentLanguage).then(refresh);
    setInterval(refresh, 5000);
  </script>
</body>
</html>
"""
