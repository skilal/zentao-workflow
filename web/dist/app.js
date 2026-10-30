"use strict";

const { useState, useEffect, useLayoutEffect, useRef } = React;
const { Layout, Menu, Card, Button, Space, Tag, Table, Input, Select, Radio, Checkbox,
  Typography, Empty, Alert, Steps, Badge, Image, Form, InputNumber, ConfigProvider,
  Drawer, Collapse, Spin, notification, Divider, Descriptions, List, Modal, Tooltip, Dropdown, Switch } = antd;
const h = React.createElement;
DOMPurify.addHook("afterSanitizeAttributes", node => {
  if (node.nodeName === "INPUT") {
    if (node.getAttribute("type") !== "checkbox") node.remove();
    else node.setAttribute("disabled", "");
  }
});
const TYPE_NAMES = { story: "需求", task: "任务", bug: "Bug" };
const STATUS_NAMES = { wait: "未开始", doing: "进行中", pause: "已暂停", done: "已完成", closed: "已关闭", cancel: "已取消", active: "已激活", draft: "草稿", reviewing: "评审中", changing: "变更中", resolved: "已解决", confirmed: "已确认", postponed: "已延期", delay: "已逾期" };
const ACTIVE = new Set(["识别分支中", "刷新分支中", "准备工作区中", "计划中", "实施中", "审查中", "修复中", "续聊处理中", "编译检查中", "编译修复中"]);
const COMPLETE = new Set(["已完成（静态审查）", "已完成（未审查）"]);
const PAGES = {
  items: ["工作项与资料", "从指派给你的需求、任务和 Bug 中，选择本次处理对象。"],
  plan: ["计划与会话", "每轮计划未提问且一次完整生成时自动实施；需要决策时等待你的答复。"],
  settings: ["工作流设置", "管理禅道账号、后端编译检查、固定 Review 提示词和默认修复轮次。"]
};

function itemKey(item) { return item ? `${item.kind}:${item.id}` : ""; }
function sessionItems(state) { return state.items || [state.item]; }
function itemLabel(state) { return sessionItems(state).map(item => `${TYPE_NAMES[item.kind]} #${item.id} · ${item.title}`).join("；"); }
function timeLabel(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : ""; }
function plainDescription(value) {
  return String(value || "").replaceAll("【spec】", "【需求描述】").replaceAll("【verify】", "【验收标准】")
    .replaceAll("【desc】", "【详细描述】").replaceAll("【steps】", "【复现步骤】").replaceAll("【bugSteps】", "【来源 Bug 描述】");
}
function typeTag(kind) { return h(Tag, { color: { story: "blue", task: "cyan", bug: "orange" }[kind] }, TYPE_NAMES[kind]); }
function stageTag(stage) {
  const color = stage?.includes("等待") ? "gold" : stage?.includes("完成") ? "success" : stage?.includes("停止") || stage?.includes("失败") ? "error" : ACTIVE.has(stage) ? "processing" : "default";
  return h(Tag, { color }, stage || "尚未选择工作项");
}
function stepOf(state) {
  if (!state) return 0;
  if (state.phase === "complete") return state.stage === "已完成（未审查）" ? 2 : 4;
  if (["followup", "compile", "compile_fix"].includes(state.phase)) return 2;
  if (state.reviews?.length || state.phase === "review" || state.phase === "fix") return 3;
  if (state.phase === "implement") return 2;
  if (state.repos?.length) return 1;
  return 0;
}
function CardBox(props) { return h(Card, { className: "app-card", ...props }, props.children); }
function Markdown({ text }) {
  const source = String(text || "").replace(/<\/?proposed_plan>/g, "");
  const html = DOMPurify.sanitize(marked.parse(source, { gfm: true, breaks: false }), {
    ALLOWED_TAGS: ["h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em", "del", "ul", "ol", "li", "blockquote", "a", "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "input"],
    ALLOWED_ATTR: ["href", "title", "class", "start", "align", "type", "checked", "disabled"],
    ALLOW_DATA_ATTR: false
  });
  return h("div", { className: "markdown-body", dangerouslySetInnerHTML: { __html: html } });
}
function Field(props) { return h("div", null, h("label", { className: "field-label" }, props.label), props.children); }
function mergeHistory(history, state) {
  if (!state) return history || [];
  const summary = Object.fromEntries(["id", "item", "items", "title", "stage", "updated_at", "fix_round", "commits", "project", "can_move_project", "project_move_reason"].map(key => [key, state[key]]));
  return [summary, ...(history || []).filter(entry => entry.id !== state.id)].sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.id.localeCompare(left.id));
}
function emptyDraft(state, settings) { return { supplement: state?.supplement || "", reply: "", review_enabled: false, answers: {}, review_note: "", selected_findings: [], review_number: state?.reviews?.at(-1)?.number || null,
  compile_check_enabled: state?.plan_started_at ? Boolean(state.compile_check?.enabled) : Boolean(settings?.compile_check_enabled),
  codex: state?.codex || { model: settings?.model ?? "", reasoning_effort: settings?.reasoning_effort ?? "" } }; }
const EFFORT_NAMES = { none: "无", minimal: "最低", low: "低", medium: "中", high: "高", xhigh: "很高", max: "最大", ultra: "超高" };
function CodexSelection({ value, onChange, onReady, disabled }) {
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [version, setVersion] = useState(0);
  useEffect(() => {
    let disposed = false;
    setLoading(true);
    setError("");
    fetch(`/api/codex-models${version ? "?refresh=1" : ""}`, { credentials: "same-origin", cache: "no-store" })
      .then(readResponse).then(result => { if (!disposed) setCatalog(result); })
      .catch(error => { if (!disposed) { setCatalog(null); setError(error.message); } })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, [version]);
  const selected = value.model === "" ? catalog?.default_model : value.model;
  const current = catalog?.models.find(entry => entry.model === selected);
  const valid = !loading && !error && current?.reasoning_efforts.includes(value.reasoning_effort);
  useEffect(() => {
    onReady(valid ? { model: selected, reasoning_effort: value.reasoning_effort } : null);
  }, [valid, selected, value.reasoning_effort, onReady]);
  const options = catalog ? [{ value: "", label: `沿用本机 Codex（${catalog.default_model || "默认模型未确定"}）` },
    ...catalog.models.map(entry => ({ value: entry.model, label: `${entry.display_name} · ${entry.model}` }))] : [];
  return h("div", { className: "codex-selection" },
    h("div", { className: "settings-grid" },
      h(Field, { label: "模型" }, h(Select, { "aria-label": "模型", className: "full-width", showSearch: true, optionFilterProp: "label",
        value: value.model, options, loading, disabled: disabled || loading || Boolean(error),
        notFoundContent: loading ? "正在读取模型…" : "暂无可用模型", onChange: model => {
          const entry = catalog.models.find(entry => entry.model === (model === "" ? catalog.default_model : model));
          onChange({ model, reasoning_effort: entry?.reasoning_efforts.includes(value.reasoning_effort) ? value.reasoning_effort : "" });
        } })),
      h(Field, { label: "推理强度" }, h(Select, { "aria-label": "推理强度", className: "full-width", placeholder: "请选择推理强度",
        value: value.reasoning_effort || undefined, disabled: disabled || loading || Boolean(error) || !current,
        options: (current?.reasoning_efforts || []).map(effort => ({ value: effort, label: `${EFFORT_NAMES[effort] || effort}（${effort}）` })),
        onChange: reasoning_effort => onChange({ ...value, reasoning_effort }) }))),
    h("div", { className: "action-bar" },
      h(Typography.Text, { type: "secondary" }, loading ? "正在读取本机 Codex 模型…" : current ? `实际模型：${selected}` : "请从当前可用列表中选择模型"),
      h(Button, { size: "small", loading, disabled: disabled || loading, onClick: () => setVersion(previous => previous + 1) }, "刷新模型列表")),
    error && h(Alert, { type: "error", showIcon: true, message: "读取模型失败", description: error }),
    !loading && !error && catalog?.models.length === 0 && h(Alert, { type: "warning", showIcon: true, message: "本机 Codex 未返回可用模型，请检查账号后刷新列表。" }),
    !loading && current && value.reasoning_effort && !valid && h(Alert, { type: "warning", showIcon: true, message: "当前模型不支持已选推理强度，请重新选择。" }));
}
function projectCatalog(model) {
  const projects = new Map((model.project_partitions || []).map(project => [project.id, { ...project }]));
  for (const entry of model.history) {
    if (entry.project?.id > 0 && !projects.has(entry.project.id)) projects.set(entry.project.id, { ...entry.project, manual: false });
  }
  for (const item of model.items) {
    if (item.project_id > 0 && !projects.has(item.project_id)) projects.set(item.project_id, { id: item.project_id, name: item.project_name, manual: false });
  }
  return [...projects.values()];
}
async function readResponse(response) {
  const value = await response.json();
  if (!response.ok) throw new Error(value.error || "本地服务未完成该操作。");
  return value;
}

function ZentaoProjectSelect({ candidates, ...props }) {
  const [projects, setProjects] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  useEffect(() => {
    let disposed = false;
    fetch("/api/zentao-projects", { credentials: "same-origin", cache: "no-store" }).then(readResponse).then(value => {
      if (!disposed) setProjects(value.projects);
    }).catch(error => { if (!disposed) setError(error.message); })
      .finally(() => { if (!disposed) setLoading(false); });
    return () => { disposed = true; };
  }, []);
  const options = projects.filter(project => !candidates?.length || candidates.some(entry => entry.id === project.id))
    .map(project => ({ value: project.id, label: `${project.name} #${project.id}`, name: project.name }));
  return h(React.Fragment, null,
    h(Select, { className: "full-width", placeholder: "搜索项目名称或编号后选择", showSearch: true,
      optionFilterProp: "label", loading, notFoundContent: loading ? "正在读取禅道项目…" : error || "暂无匹配项目",
      ...props, options, disabled: props.disabled || loading || Boolean(error) }),
    error && h(Typography.Text, { type: "danger" }, error));
}

function WorkflowApp() {
  const [model, setModel] = useState(null);
  const [navCollapsed, setNavCollapsed] = useState(true);
  const modelRef = useRef(null);
  const csrf = useRef("");
  const [page, setPage] = useState(() => {
    const saved = sessionStorage.getItem("workflow-page");
    if (saved === "review") { sessionStorage.setItem("workflow-page", "plan"); return "plan"; }
    return PAGES[saved] ? saved : "items";
  });
  const [online, setOnline] = useState(false);
  const [stopped, setStopped] = useState(false);
  const [projectInfoHidden, setProjectInfoHidden] = useState(() => sessionStorage.getItem("workflow-project-info-hidden") === "true");
  const eventSource = useRef(null);
  const [fatal, setFatal] = useState("");
  const [requestsPending, setRequestsPending] = useState({});
  const sending = requestsPending[model?.selectedSessionId || model?.preview?.preview_id || "new"] || requestsPending.new || requestsPending.global || "";
  const [sessionQuery, setSessionQuery] = useState("");
  const [sessionFilter, setSessionFilter] = useState("all");
  const [collapsedProjects, setCollapsedProjects] = useState(() => JSON.parse(sessionStorage.getItem("workflow-collapsed-projects") || "[]"));
  const [dragSessionId, setDragSessionId] = useState(null);
  const [dropProjectId, setDropProjectId] = useState(null);
  const viewEpoch = useRef(0);
  const deletedSessions = useRef(new Set());
  const [selectedItems, setSelectedItems] = useState([]);
  const selectionRequest = useRef("");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [draft, setDraft] = useState(emptyDraft());
  const draftRef = useRef(draft);
  const imageInput = useRef(null);
  const followupImageInput = useRef(null);
  const imageQueues = useRef({});
  const [imageTransfers, setImageTransfers] = useState({});
  const [diffOpen, setDiffOpen] = useState(false);
  const [diffs, setDiffs] = useState(null);
  const [settingsForm] = Form.useForm();
  const settingsModel = Form.useWatch("model", settingsForm);
  const settingsEffort = Form.useWatch("reasoning_effort", settingsForm);
  const [settingsSelection, setSettingsSelection] = useState(null);
  const [sessionSelection, setSessionSelection] = useState(null);
  const feedRef = useRef(null);
  const lastSession = useRef(null);
  const lastSettings = useRef("");
  const [notice, noticeHolder] = notification.useNotification();
  const [modal, modalHolder] = Modal.useModal();

  function applyModel(value) {
    const selected = value.selectedSessionId ? value.sessions?.[value.selectedSessionId] : value.preview;
    const projected = { ...value, state: selected?.state || null, revision: selected?.revision,
      stream: selected?.stream || "", busy: selected?.busy || value.global_busy };
    modelRef.current = projected;
    setModel(projected);
  }
  function navigate(key) { setPage(key); sessionStorage.setItem("workflow-page", key); window.scrollTo(0, 0); }
  function changeDraft(change) {
    const value = { ...draftRef.current, ...change };
    draftRef.current = value;
    setDraft(value);
    if (modelRef.current?.selectedSessionId) sessionStorage.setItem(`workflow-draft:${modelRef.current.selectedSessionId}`, JSON.stringify(value));
  }
  function viewPayload(id) {
    const current = modelRef.current;
    return current.preview?.preview_id === id ? current.preview : current.sessions[id];
  }
  function viewAddress(id) {
    return modelRef.current.preview?.preview_id === id ? { preview_id: id } : { session_id: id };
  }
  function receivePreview(payload) {
    const current = modelRef.current;
    if (current.preview?.preview_id !== payload.preview_id || current.preview.event_cursor > payload.event_cursor) return;
    applyModel({ ...current, preview: payload });
  }
  function releasePreview(id) {
    fetch("/api/previews/discard", { method: "POST", credentials: "same-origin", keepalive: true,
      headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
      body: JSON.stringify({ preview_id: id }) }).then(readResponse)
      .catch(error => notice.error({ message: "临时资料未释放", description: error.message }));
  }
  function discardPreview() {
    const id = modelRef.current?.preview?.preview_id;
    if (!id) return;
    delete imageQueues.current[id];
    publishImageTransfers();
    applyModel({ ...modelRef.current, preview: null });
    releasePreview(id);
  }
  function publishImageTransfers() {
    setImageTransfers(Object.fromEntries(Object.entries(imageQueues.current).map(([id, queue]) => [id, [...queue.tasks]])));
  }
  async function uploadImages(id) {
    const queue = imageQueues.current[id];
    if (queue.running) return;
    queue.running = true;
    try {
      let task;
      while ((task = queue.tasks.find(entry => entry.status === "waiting"))) {
        task.status = "uploading";
        publishImageTransfers();
        try {
          const current = viewPayload(id);
          if (!current) throw new Error("查看资料已释放或会话已删除，请重新选择工作项。");
          const query = new URLSearchParams({ ...viewAddress(id), revision: current.revision, name: task.name, purpose: task.purpose });
          const payload = await fetch(`/api/supplement-images?${query}`, { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/octet-stream", "X-Workflow-Token": csrf.current }, body: task.file }).then(readResponse);
          if (payload.preview_id) receivePreview(payload);
          else if (modelRef.current.sessions[id]) receiveState(payload);
          queue.tasks = queue.tasks.filter(entry => entry.uid !== task.uid);
        } catch (error) { task.status = "error"; task.error = error.message; }
        publishImageTransfers();
      }
    } finally { queue.running = false; }
  }
  function addImages(files, purpose = "supplement") {
    const id = modelRef.current.state?.id;
    if (!id || !(purpose === "followup" ? followupEditable : supplementEditable)) return;
    const queue = imageQueues.current[id] ||= { tasks: [], running: false };
    queue.tasks.push(...Array.from(files, file => ({ uid: crypto.randomUUID(), file, name: file.name,
      status: "waiting", error: "", purpose })));
    publishImageTransfers();
    uploadImages(id);
  }
  function pasteImages(event, purpose = "supplement") {
    const files = Array.from(event.clipboardData.items).filter(item => item.kind === "file" && item.type.startsWith("image/"))
      .map(item => item.getAsFile()).filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain");
    const input = event.target;
    if (text && input.tagName === "TEXTAREA") {
      const start = input.selectionStart, end = input.selectionEnd;
      changeDraft({ [purpose === "followup" ? "reply" : "supplement"]: input.value.slice(0, start) + text + input.value.slice(end) });
      requestAnimationFrame(() => input.setSelectionRange(start + text.length, start + text.length));
    }
    addImages(files, purpose);
  }
  async function removeImage(image, purpose = "supplement") {
    const id = state.id;
    setRequestsPending(previous => ({ ...previous, [id]: "remove_image" }));
    try {
      const payload = await fetch("/api/supplement-images/remove", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ ...viewAddress(id), revision: viewPayload(id).revision, image_id: image.id, purpose }) }).then(readResponse);
      if (payload.preview_id) receivePreview(payload);
      else if (modelRef.current.sessions[id]) receiveState(payload);
    } catch (error) { notice.error({ message: "图片未移除", description: error.message }); }
    finally { setRequestsPending(previous => { const next = { ...previous }; delete next[id]; return next; }); }
  }
  function receiveState(payload) {
    const previous = modelRef.current;
    if (deletedSessions.current.has(payload.session_id)) return;
    const cached = previous.sessions[payload.session_id];
    if (cached && cached.event_cursor > payload.event_cursor) return;
    if (previous.selectedSessionId === payload.session_id && previous.state?.phase === "branch" && payload.state?.phase === "plan") navigate("plan");
    const history = mergeHistory(previous.history, payload.state).map(entry => entry.id === payload.session_id ?
      { ...entry, busy: payload.busy, working: payload.working, deleting: payload.busy === "delete_session" } : entry);
    applyModel({ ...previous, sessions: { ...previous.sessions, [payload.session_id]: payload }, history });
  }
  function newSession() {
    viewEpoch.current += 1;
    discardPreview();
    lastSession.current = null;
    selectionRequest.current = "";
    setSelectedItems([]);
    draftRef.current = emptyDraft(null, modelRef.current.settings);
    setDraft(draftRef.current);
    sessionStorage.setItem("workflow-session", "");
    applyModel({ ...modelRef.current, selectedSessionId: null });
    setDiffOpen(false);
    navigate("items");
  }
  async function selectSession(id, targetPage) {
    const epoch = ++viewEpoch.current;
    discardPreview();
    selectionRequest.current = "";
    sessionStorage.setItem("workflow-session", id);
    applyModel({ ...modelRef.current, selectedSessionId: id });
    setDiffOpen(false);
    if (targetPage) navigate(targetPage);
    try {
      const payload = await fetch(`/api/session?id=${encodeURIComponent(id)}`, { cache: "no-store" }).then(readResponse);
      if (epoch !== viewEpoch.current || modelRef.current.selectedSessionId !== id) return;
      receiveState(payload);
    } catch (error) { if (epoch === viewEpoch.current) notice.error({ message: "无法查看会话", description: error.message }); }
  }
  async function deleteSession(id) {
    try {
      const payload = await fetch(`/api/session?id=${encodeURIComponent(id)}`, { cache: "no-store" }).then(readResponse);
      receiveState(payload);
      const removed = payload.state.cleanup?.removed || [];
      modal.confirm({ title: "删除会话及其新建工作区？", okText: "删除会话", cancelText: "取消", okButtonProps: { danger: true }, width: 620,
        content: h("div", { className: "delete-description" }, h("p", null, itemLabel(payload.state)),
          h(Alert, { type: "warning", showIcon: true, message: "会话记录、附件和本会话新建工作区将被删除；复用目录、Git 分支及提交保留。新建工作区存在未提交文件或被其他会话使用时会停止删除。" }),
          h("p", null, "会话资料目录："), h("div", { className: "repo-path" }, payload.directory),
          h("p", null, "本会话新建工作区："), payload.workspaces.length ? payload.workspaces.map(path => h("div", { key: path, className: "repo-path" }, path, removed.includes(path) && "（已清理）")) : h("p", { className: "muted" }, "没有需要删除的工作区"),
          payload.reused_workspaces?.length > 0 && h(React.Fragment, null, h("p", null, "以下复用目录将保留："), payload.reused_workspaces.map(path => h("div", { key: path, className: "repo-path" }, path)))),
        onOk: async () => { if (!await act("delete_session", {}, id)) throw new Error("删除操作未提交"); } });
    } catch (error) { notice.error({ message: "无法删除会话", description: error.message }); }
  }
  function renameSession(entry) {
    let title = entry.title;
    modal.confirm({ title: "修改会话标题", okText: "保存", cancelText: "取消", icon: null,
      content: h(Field, { label: "会话标题" }, h(Input, { defaultValue: title, "aria-label": "会话标题",
        placeholder: "请输入会话标题", onChange: event => { title = event.target.value; } })),
      onOk: async () => {
        setRequestsPending(previous => ({ ...previous, [entry.id]: "rename_session" }));
        try {
          const payload = await fetch("/api/sessions/title", { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
            body: JSON.stringify({ session_id: entry.id, title, request_id: crypto.randomUUID() }) }).then(readResponse);
          receiveState(payload);
        } catch (error) { notice.error({ message: "标题未保存", description: error.message }); throw error; }
        finally { setRequestsPending(previous => { const next = { ...previous }; delete next[entry.id]; return next; }); }
      } });
  }
  function changeProjectCollapse(keys) {
    setCollapsedProjects(keys);
    sessionStorage.setItem("workflow-collapsed-projects", JSON.stringify(keys));
  }
  function addProjectPartition() {
    let projectId;
    const dialog = modal.confirm({ title: "添加项目分区", okText: "添加", cancelText: "取消", icon: null,
      okButtonProps: { disabled: true },
      content: h("div", null, h("p", { className: "muted" }, "选择当前账号可见的禅道项目，添加到本地列表。支持搜索项目名称或编号。"),
        h(Field, { label: "禅道项目" }, h(ZentaoProjectSelect, { "aria-label": "新增分区的禅道项目", onChange: value => {
          projectId = value; dialog.update({ okButtonProps: { disabled: false } });
        } }))),
      onOk: async () => {
        try {
          if (!Number.isInteger(projectId) || projectId <= 0) throw new Error("请选择禅道项目。");
          const result = await fetch("/api/projects", { method: "POST", credentials: "same-origin",
            headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
            body: JSON.stringify({ project_id: projectId, request_id: crypto.randomUUID() }) }).then(readResponse);
          applyModel({ ...modelRef.current, project_partitions: result.project_partitions });
          changeProjectCollapse(collapsedProjects.filter(key => key !== String(result.project.id)));
        } catch (error) { notice.error({ message: "项目分区未添加", description: error.message }); throw error; }
      } });
  }
  async function moveProject(id, projectId) {
    const current = modelRef.current;
    const payload = current.preview?.preview_id === id ? current.preview : current.sessions[id];
    setRequestsPending(previous => ({ ...previous, [id]: "move_project" }));
    try {
      const result = await fetch("/api/sessions/project", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ ...viewAddress(id), revision: payload.revision, project_id: projectId,
          request_id: crypto.randomUUID() }) }).then(readResponse);
      if (result.preview_id) receivePreview(result);
      else receiveState(result);
      changeProjectCollapse(collapsedProjects.filter(key => key !== String(projectId || 0)));
    } catch (error) { notice.error({ message: "项目未更换", description: error.message }); }
    finally { setRequestsPending(previous => { const next = { ...previous }; delete next[id]; return next; }); }
  }
  useEffect(() => {
    let events, disposed = false;
    fetch("/api/bootstrap", { credentials: "same-origin", cache: "no-store" }).then(readResponse).then(data => {
      if (disposed) return;
      if (data.preview_supported !== true) throw new Error("本地服务版本过旧，请停止并重新启动本地服务，再刷新页面。当前服务不支持临时查看资料。");
      csrf.current = data.csrf_token;
      const saved = sessionStorage.getItem("workflow-session");
      const selectedSessionId = saved === null ? data.default_session_id : saved;
      applyModel({ ...data, selectedSessionId: data.sessions[selectedSessionId] ? selectedSessionId : null, global_busy: data.busy });
      events = new EventSource(`/api/events?cursor=${data.cursor}`);
      eventSource.current = events;
      events.onopen = () => setOnline(true);
      events.onerror = () => setOnline(false);
      for (const kind of ["state", "preview", "items", "connected", "connection", "conversation", "delta", "operation_error", "questions", "busy", "settings", "snapshot", "session_deleted", "projects"]) {
        events.addEventListener(kind, event => {
          if (disposed) return;
          const value = JSON.parse(event.data);
          const current = modelRef.current;
          if (kind === "state") receiveState(value);
          else if (kind === "preview") receivePreview(value);
          else if (kind === "snapshot") {
            const missing = current.selectedSessionId && !value.sessions[current.selectedSessionId];
            for (const id of Object.keys(current.sessions)) if (!value.sessions[id]) {
              sessionStorage.removeItem(`workflow-draft:${id}`);
              deletedSessions.current.add(id);
            }
            applyModel({ ...current, ...value, selectedSessionId: missing ? null : current.selectedSessionId, global_busy: value.busy, csrf_token: csrf.current });
            if (current.preview) {
              const id = current.preview.preview_id;
              fetch(`/api/preview?id=${encodeURIComponent(id)}`, { cache: "no-store" }).then(readResponse).then(receivePreview)
                .catch(error => notice.error({ message: "临时资料同步失败", description: error.message }));
            }
            if (missing) newSession();
          }
          else if (kind === "projects") applyModel({ ...current, project_partitions: value.project_partitions });
          else if (kind === "items") applyModel({ ...current, items: value });
          else if (kind === "connected") applyModel({ ...current, connected: true, profile: value });
          else if (kind === "connection") applyModel({ ...current, ...value });
          else if (kind === "busy") {
            if (value.session_id) receiveState(value);
            else applyModel({ ...current, global_busy: value.busy });
          }
          else if (kind === "settings") applyModel({ ...current, settings: value });
          else if (kind === "conversation") receiveState(value);
          else if (kind === "delta") {
            const cached = current.sessions[value.session_id];
            if (cached && cached.event_cursor < value.event_cursor) applyModel({ ...current, sessions: { ...current.sessions,
              [value.session_id]: { ...cached, stream: cached.stream + value.delta, event_cursor: value.event_cursor } } });
          }
          else if (kind === "questions" && current.selectedSessionId === value.session_id) navigate("plan");
          else if (kind === "session_deleted") {
            delete imageQueues.current[value.session_id];
            publishImageTransfers();
            deletedSessions.current.add(value.session_id);
            sessionStorage.removeItem(`workflow-draft:${value.session_id}`);
            const sessions = { ...current.sessions };
            delete sessions[value.session_id];
            applyModel({ ...current, sessions, history: current.history.filter(entry => entry.id !== value.session_id) });
            if (current.selectedSessionId === value.session_id) newSession();
          }
          else if (kind === "operation_error") {
            applyModel({ ...current, connected: value.connected });
            notice.error({ message: value.session_id && value.session_id !== current.selectedSessionId ? "其他会话操作未完成" : "操作未完成", description: value.message, duration: 7 });
          }
        });
      }
    }).catch(error => { if (!disposed) setFatal(error.message); });
    return () => { disposed = true; if (events) events.close(); };
  }, []);
  useEffect(() => {
    window.addEventListener("pagehide", discardPreview);
    return () => window.removeEventListener("pagehide", discardPreview);
  }, []);

  const state = model?.state;
  const stage = state?.stage || "";
  const review = state?.reviews?.filter(entry => (entry.delivery_round || 1) === (state.delivery_round || 1)).at(-1);
  const transfers = imageTransfers[state?.id] || [];
  const imagesUploading = transfers.some(entry => entry.status !== "error");
  const supplementEditable = Boolean(state && ["资料已读取", "准备工作区失败"].includes(stage) &&
    !stopped && !sending && !model?.busy && !state.cleanup?.removed?.length && !state.cleanup?.finalizing);
  const followupEditable = Boolean(state?.phase === "complete" && COMPLETE.has(stage) &&
    !stopped && !sending && !model?.busy && !state.cleanup?.removed?.length && !state.cleanup?.finalizing);
  const busy = Boolean(stopped || sending || model?.busy || state?.cleanup?.removed?.length || state?.cleanup?.finalizing || imagesUploading);
  const selectedKey = selectedItems.join("|");
  const loadedKey = state ? sessionItems(state).map(itemKey).join("|") : "";
  const selectionMatches = Boolean(state?.snapshot) && selectedItems.length > 0 && selectedKey === loadedKey;
  useEffect(() => {
    if (!state || state.id === lastSession.current) return;
    lastSession.current = state.id;
    selectionRequest.current = "";
    const itemKeys = sessionItems(state).map(itemKey);
    const saved = model.selectedSessionId ? sessionStorage.getItem(`workflow-draft:${state.id}`) : null;
    const value = { ...emptyDraft(state, model.settings), ...(saved ? JSON.parse(saved) : {}) };
    setSelectedItems(state.can_edit && value.selected_items ? value.selected_items : itemKeys);
    draftRef.current = value;
    setDraft(value);
  }, [state?.id]);
  useEffect(() => {
    if (!selectedItems.length || selectionMatches || busy || !model?.connected || (state && !state.can_edit) ||
        (state && state.id !== lastSession.current) || selectionRequest.current === selectedKey) return;
    const timer = setTimeout(() => {
      selectionRequest.current = selectedKey;
      act("collect", { items: selectedItems.map(key => {
        const [kind, id] = key.split(":");
        return { kind, id: Number(id) };
      }) });
    }, 300);
    return () => clearTimeout(timer);
  }, [selectedKey, loadedKey, busy, model?.connected]);
  useEffect(() => {
    if (!state || !review) return;
    if (draftRef.current.review_number !== review.number) changeDraft({ selected_findings: [], review_note: "", review_number: review.number });
  }, [state?.id, review?.number]);
  useLayoutEffect(() => {
    if (feedRef.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [page, state?.id, selectionMatches, state?.phase, model?.stream, state?.history?.length, review?.number]);
  useEffect(() => {
    if (!model?.settings) return;
    const serialized = JSON.stringify(model.settings);
    if (lastSettings.current !== serialized) {
      lastSettings.current = serialized;
      settingsForm.setFieldsValue({ ...model.settings, password: "" });
    }
  }, [model?.settings]);

  async function act(action, data = {}, targetId = modelRef.current.selectedSessionId) {
    const current = modelRef.current;
    const id = ["connect", "refresh", "settings"].includes(action) ? null : targetId;
    const previewId = !id && ["collect", "plan"].includes(action) ? current.preview?.preview_id : null;
    const pendingKey = id || previewId || (action === "collect" ? "new" : "global");
    const epoch = viewEpoch.current;
    setRequestsPending(previous => ({ ...previous, [pendingKey]: action }));
    try {
      const result = await fetch("/api/actions", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ action, data, request_id: crypto.randomUUID(), session_id: id,
          preview_id: previewId, revision: previewId ? current.preview.revision : current.sessions[id]?.revision }) }).then(readResponse);
      if (action === "collect" && !id && epoch === viewEpoch.current) {
        if (!previewId) changeDraft({ codex: { model: current.settings.model, reasoning_effort: current.settings.reasoning_effort } });
        lastSession.current = result.preview_id;
        applyModel({ ...modelRef.current, preview: { preview_id: result.preview_id, event_cursor: -1 } });
        const payload = await fetch(`/api/preview?id=${encodeURIComponent(result.preview_id)}`, { cache: "no-store" }).then(readResponse);
        if (epoch === viewEpoch.current) receivePreview(payload);
      } else if (action === "collect" && !id && !previewId) {
        releasePreview(result.preview_id);
      } else if (action === "plan" && previewId && epoch === viewEpoch.current) {
        lastSession.current = result.session_id;
        delete imageQueues.current[previewId];
        publishImageTransfers();
        sessionStorage.setItem("workflow-session", result.session_id);
        sessionStorage.setItem(`workflow-draft:${result.session_id}`, JSON.stringify(draftRef.current));
        applyModel({ ...modelRef.current, preview: null, selectedSessionId: result.session_id });
        const payload = await fetch(`/api/session?id=${encodeURIComponent(result.session_id)}`, { cache: "no-store" }).then(readResponse);
        if (epoch === viewEpoch.current) receiveState(payload);
      }
      return result;
    } catch (error) { notice.error({ message: "操作未提交", description: error.message }); return null; }
    finally { setRequestsPending(previous => { const next = { ...previous }; delete next[pendingKey]; return next; }); }
  }
  async function startPlan() {
    if (!selectionMatches || busy || imageQueues.current[state.id]?.tasks.length || (!state.codex && !sessionSelection)) return;
    const epoch = viewEpoch.current;
    const result = await act("plan", { ...(state.codex || sessionSelection), compile_check_enabled: state.plan_started_at ? Boolean(state.compile_check?.enabled) : draft.compile_check_enabled,
      supplement: draft.supplement, items: selectedItems.map(key => {
      const [kind, number] = key.split(":"); return { kind, id: Number(number) };
    }) });
    if (result && epoch === viewEpoch.current && modelRef.current.selectedSessionId === result.session_id) navigate("plan");
  }
  async function answer() {
    if (state.phase === "complete") {
      const images = state.followup_images.filter(image => image.history_index === undefined);
      if (busy || transfers.length) return;
      if (!draft.reply.trim() && !images.length) { notice.warning({ message: "请填写消息或添加图片" }); return; }
      const id = state.id;
      const result = await act("followup", { text: draft.reply, image_ids: images.map(image => image.id), review_enabled: draft.review_enabled });
      if (result) {
        if (modelRef.current.selectedSessionId === id) changeDraft({ reply: "", answers: {}, review_enabled: false });
        else {
          const saved = JSON.parse(sessionStorage.getItem(`workflow-draft:${id}`) || "{}");
          sessionStorage.setItem(`workflow-draft:${id}`, JSON.stringify({ ...saved, reply: "", answers: {}, review_enabled: false }));
        }
      }
      return;
    }
    const questions = state?.pending?.params?.questions || [];
    const answers = {}, pieces = [];
    for (const question of questions) {
      const key = `${state.pending.id}:${question.id}:${question.question}`;
      const value = draft.answers[key] || {};
      const text = [value.choice, value.custom?.trim()].filter(Boolean).join("\n");
      if (!text) { notice.warning({ message: "尚未回答完整", description: "请逐项明确选择或填写答案。" }); return; }
      answers[question.id] = [text];
      pieces.push(`${question.question}\n回答：${text}`);
    }
    const text = state.pending?.method === "workflow/ask_user" ? draft.reply.trim() :
      [pieces.join("\n\n"), draft.reply?.trim()].filter(Boolean).join("\n\n");
    if (!text && !questions.length) { notice.warning({ message: "请先填写回复" }); return; }
    const id = state.id;
    if (await act("answer", { text, answers: questions.length ? answers : null }) && modelRef.current.selectedSessionId === id) changeDraft({ reply: "", answers: {} });
  }
  async function openDiff() {
    const epoch = viewEpoch.current;
    const id = state.id;
    setDiffOpen(true); setDiffs(null);
    try {
      const value = await fetch(`/api/diff?session_id=${encodeURIComponent(id)}`).then(readResponse);
      if (epoch === viewEpoch.current) setDiffs(value);
    } catch (error) { if (epoch === viewEpoch.current) { setDiffOpen(false); notice.error({ message: "无法读取差异", description: error.message }); } }
  }
  async function stopService() {
    setRequestsPending(previous => ({ ...previous, global: "shutdown" }));
    try {
      await fetch("/api/shutdown", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current }, body: "{}" }).then(readResponse);
      if (eventSource.current) eventSource.current.close();
      setStopped(true); setOnline(false);
      const current = modelRef.current;
      const sessions = Object.fromEntries(Object.entries(current.sessions).map(([id, value]) => [id, { ...value, busy: null, working: false,
        state: { ...value.state, stage: ACTIVE.has(value.state.stage) || value.state.stage === "等待人工回答" ? "已暂停" : value.state.stage } }]));
      applyModel({ ...current, sessions, global_busy: null, history: current.history.map(entry => ({ ...entry, busy: null, working: false,
        stage: ACTIVE.has(entry.stage) || entry.stage === "等待人工回答" ? "已暂停" : entry.stage })) });
    } catch (error) { notice.error({ message: "未能停止服务", description: error.message }); }
    finally { setRequestsPending(previous => { const next = { ...previous }; delete next.global; return next; }); }
  }

  function renderItems() {
    const rows = model.items.filter(item => (filter === "all" || item.kind === filter) &&
      (!query.trim() || `${item.id} ${item.title}`.toLowerCase().includes(query.trim().toLowerCase())));
    const canCollect = !state || state.can_edit;
    const selectionDisabled = busy || !canCollect || !model.connected;
    function chooseItems(keys) {
      if (selectionDisabled) return;
      const added = keys.find(key => !selectedItems.includes(key));
      const next = added && !added.startsWith("task:") ? [added] : keys.filter(key => key.startsWith("task:"));
      selectionRequest.current = "";
      setSelectedItems(next);
      changeDraft({ selected_items: next });
    }
    const columns = [
      { title: "类型", dataIndex: "kind", width: 85, render: typeTag },
      { title: "编号", dataIndex: "id", width: 90, render: value => h(Typography.Text, { code: true }, `#${value}`) },
      { title: "标题", dataIndex: "title", render: value => h("span", { className: "table-title" }, value) },
      { title: "状态", dataIndex: "status", width: 100, render: value => h(Tag, null, STATUS_NAMES[value] || value) },
      { title: "所属项目", dataIndex: "project", width: 120, render: value => /^\d+$/.test(String(value)) ? `项目 #${value}` : String(value || "—") },
      { title: "优先级", dataIndex: "priority", width: 80, render: value => value ? `P${value}` : "—" }
    ];
    const snapshot = selectionMatches ? state?.snapshot : null;
    return h("div", { className: "card-stack" },
      h(CardBox, { title: "待处理工作项", extra: h(Space, null, h(Typography.Text, { type: "secondary" }, `共 ${model.items.length} 项`), h(Button, { onClick: () => act("refresh"), disabled: busy || !model.connected, loading: model.busy === "refresh" }, "刷新列表")) },
        h("div", { className: "table-tools" }, h(Radio.Group, { value: filter, onChange: event => setFilter(event.target.value), optionType: "button", options: [{ label: "全部", value: "all" }, ...Object.entries(TYPE_NAMES).map(([value, label]) => ({ value, label: `${label} ${model.items.filter(item => item.kind === value).length}` }))] }),
          h(Input.Search, { placeholder: "搜索编号或标题", allowClear: true, value: query, onChange: event => setQuery(event.target.value), style: { width: 260 } })),
        h(Table, { columns, dataSource: rows, rowKey: itemKey, size: "middle", pagination: rows.length > 10 ? { pageSize: 10, showSizeChanger: false } : false,
          scroll: { x: 700 }, locale: { emptyText: model.connected ? "暂无符合条件的待处理工作项" : "连接禅道后显示待处理工作项" }, rowClassName: "item-row",
          rowSelection: { type: "checkbox", selectedRowKeys: selectedItems, preserveSelectedRowKeys: true, hideSelectAll: filter !== "task", onChange: chooseItems,
            getCheckboxProps: () => ({ disabled: selectionDisabled }) },
          onRow: record => ({ onClick: event => {
            if (event.target.closest(".ant-table-selection-column")) return;
            const key = itemKey(record);
            chooseItems(selectedItems.includes(key) ? selectedItems.filter(value => value !== key) : [...selectedItems, key]);
          } }) }),
        h("div", { className: "data-foot" }, h("span", { className: "muted" }, "仅显示直接指派给当前登录账号的待处理工作项"),
          h(Space, null, (sending === "collect" || model.busy === "collect") && h(Spin, { size: "small" }),
            h("span", { className: "muted" }, selectedItems.length ? `已选 ${selectedItems.length} 项 · 选中后自动读取资料` : "可多选同一项目的任务联合开发；需求和 Bug 单选")))),
      h(CardBox, { title: snapshot ? h(Space, { wrap: true }, "工作项资料", ...sessionItems(state).map(item => h(Tag, { key: itemKey(item), color: "blue" }, `${TYPE_NAMES[item.kind]} #${item.id}`))) : "工作项资料" },
        stage === "资料读取失败" && h(Alert, { type: "error", showIcon: true, message: "资料读取失败", description: state.error,
          action: h(Button, { disabled: busy || !model.connected, onClick: () => act("collect", { items: selectedItems.map(key => {
            const [kind, id] = key.split(":"); return { kind, id: Number(id) };
          }) }) }, "重新读取资料"), style: { marginBottom: 16 } }),
        !snapshot ? h("div", { className: "empty-area" }, h(Empty, { description: model.busy === "collect" || sending === "collect" ? "正在读取所选工作项的完整资料…" : "勾选工作项后自动读取描述、图片、附件及关联需求" })) :
          h(React.Fragment, null,
            snapshot.blockers?.length > 0 && h(Alert, { type: "warning", showIcon: true, message: "有待补充的需求资料", description: h("div", null, snapshot.blockers.map((text, index) => h("div", { key: index }, text))), style: { marginBottom: 20 } }),
            snapshot.sources.map((source, index) => h("div", { className: "source-block", key: `${source.kind}:${source.id}:${index}` },
              h("div", { className: "source-heading" }, h(Tag, { color: source.selected ? "blue" : "default" }, source.selected || (source.selected == null && index === 0) ? "选中工作项" : "关联资料"), h("strong", null, source.title), h(Typography.Text, { type: "secondary" }, `${TYPE_NAMES[source.kind]} #${source.id}`),
                source.version != null && h(Tag, null, `关联版本 ${source.version}`)),
              source.note && h(Typography.Text, { type: "secondary" }, source.note),
              h("pre", { className: "description-text" }, plainDescription(source.content) || (source.origin === "task" ? "任务详情未提供关联需求正文，可结合任务描述分析" : "该工作项没有有效描述，请在计划前补充目标和验收标准。")),
              h("div", { className: "attachment-grid" }, snapshot.materials.filter(material => source.material_ids.includes(material.file_id)).map(material => {
              const url = `/api/material?${new URLSearchParams({ ...(model.preview ? { preview_id: state.id } : { session: state.id }), file_id: material.file_id })}`;
              return h("div", { className: "attachment", key: material.file_id }, material.image && h(Image, { src: url, alt: material.title, preview: { mask: "查看图片" } }),
                h("div", { className: "attachment-name" }, material.image ? `${material.title} · ${material.file_id}` : h("a", { href: url }, `${material.title}.${material.extension}`)),
                material.issue && h(Typography.Text, { type: "warning" }, material.issue));
            })))),
            snapshot.feishu_links?.length > 0 && h(Alert, { type: "info", showIcon: true, message: "飞书文档暂未接入", style: { marginTop: 18 },
              description: h("div", null, h("p", null, "如果文档中有必要业务规则，请在计划前补充相关内容。"), snapshot.feishu_links.map(url => h("div", { key: url }, h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url)))) }))),
      snapshot && renderBranches());
  }

  function renderProjectInfo() {
    const project = state.project || state.snapshot.project || {};
    const repositories = state.branch_resolution?.repositories || [];
    const editable = ["资料已读取", "准备工作区失败"].includes(stage) && !busy && !state.repos.length;
    const waiting = stage === "等待选择分支";
    const unresolved = repositories.filter(entry => entry.status === "needs_user");
    const choices = draft.branch_choices || {};
    const projectId = draft.project_id ?? project.id;
    const projectName = draft.project_name ?? project.name ?? "";
    return h("aside", { className: "project-info-panel", id: "project-info", "aria-label": "项目信息" },
      h(CardBox, { size: "small", className: "app-card project-info-card", title: "项目信息",
        extra: h(Button, { type: "text", size: "small", "aria-label": "隐藏项目信息", onClick: () => {
          setProjectInfoHidden(true); sessionStorage.setItem("workflow-project-info-hidden", "true");
        } }, "隐藏") },
        h("div", { className: "project-info-sections" },
          h("section", { className: "project-info-section" }, h("div", { className: "project-info-label" }, "所属项目"),
            h("div", { className: "project-info-name" }, project.id > 0 ? project.name || `项目 #${project.id}` : "无项目对话"),
            project.id > 0 && h(Tag, { color: "blue" }, `项目 #${project.id}`),
            model.preview && !model.selectedSessionId && h("div", { className: "project-info-select" },
              h(Select, { className: "full-width", "aria-label": "查看阶段所属项目", showSearch: true, optionFilterProp: "label",
                value: project.id || 0, disabled: busy || !state.can_edit,
                options: [...projectCatalog(model).map(value => ({ value: value.id, label: `${value.name || "项目"} #${value.id}` })),
                  { value: 0, label: "无项目对话" }], onChange: id => moveProject(state.id, id || null) }),
              h("p", { className: "project-info-note" }, "开始需求分析与计划后锁定所属项目。")),
            stage === "等待确认项目" && h("div", { className: "project-info-form" },
              h("p", { className: "project-info-note" }, "搜索并选择本次所属的禅道项目。"),
              h(ZentaoProjectSelect, { "aria-label": "所属项目", value: projectId || undefined, disabled: busy,
                candidates: project.candidates,
                onChange: (id, option) => changeDraft({ project_id: id, project_name: option.name }) }),
              h(Button, { className: "project-info-confirm", type: "primary", block: true, disabled: busy || !projectId || !projectName.trim(),
                onClick: () => act("confirm_project", { project_id: projectId, name: projectName }) }, "确认项目并继续"))),
          model.settings.repositories.map(repository => {
            const entry = repositories.find(entry => entry.role === repository.role);
            const workspace = state.repos.find(entry => entry.name === repository.name);
            const branch = entry?.branch || workspace?.branch;
            return h("section", { className: "project-info-section", key: repository.role },
              h("div", { className: "project-info-section-heading" },
                h("span", { className: "project-info-label" }, repository.role === "frontend" ? "前端仓库" : "后端仓库"),
                entry?.source && h(Tag, { color: entry.source === "ai" ? "blue" : "green" }, { ai: "AI 判断", common: "项目常用", human: "人工确认" }[entry.source])),
              h(Typography.Paragraph, { className: "project-info-path", title: repository.path, copyable: { text: repository.path, tooltips: ["复制仓库路径", "已复制"] } }, repository.path),
              h("div", { className: "project-info-label" }, "目标分支"),
              waiting && entry?.status === "needs_user" ?
                h(Select, { className: "full-width project-info-select", "aria-label": `${repository.name}目标分支`, value: choices[entry.role], placeholder: "搜索并选择项目分支", showSearch: true, optionFilterProp: "label", disabled: busy,
                  notFoundContent: "没有可用分支，请准备后重新识别", options: entry.catalog.map(branch => ({ value: branch.name, label: `${branch.name}${branch.local ? "" : "（远程）"}` })),
                  onChange: branch => changeDraft({ branch_choices: { ...choices, [entry.role]: branch } }) }) :
              branch ? h("div", { className: "project-info-branch" }, branch) :
                h("div", { className: "project-info-pending" }, entry?.status === "needs_user" ? "等待人工选择" : "开始计划时自动识别"));
          })),
        (editable || waiting) && h("div", { className: "project-info-actions" },
          editable && h(Button, { block: true, disabled: transfers.length > 0, onClick: () => act("edit_branches", { supplement: draft.supplement }) }, "更换项目常用分支"),
          waiting && h(React.Fragment, null,
            h("p", { className: "project-info-note" }, "人工选择将保存为项目常用分支，后续优先复用。"),
            h(Button, { type: "primary", block: true, disabled: busy || unresolved.some(entry => !entry.catalog.some(branch => branch.name === choices[entry.role])),
              onClick: () => act("choose_branches", { choices: Object.fromEntries(unresolved.map(entry => [entry.role, choices[entry.role]])) }) }, "保存为项目常用分支并继续"),
            h(Button, { type: "text", block: true, disabled: busy || Boolean(state.repos.length), onClick: startPlan }, "重新识别分支")))));
  }

  function renderBranches() {
    const transfers = (imageTransfers[state.id] || []).filter(task => task.purpose !== "followup");
    const editable = ["资料已读取", "准备工作区失败"].includes(stage) && !busy;
    const repositories = state.branch_resolution?.repositories || [];
    const detailRepositories = state.branch_resolution?.manual && !state.repos.length ? [] : model.settings.repositories.filter(repository => {
      const entry = repositories.find(entry => entry.role === repository.role);
      return state.repos.some(entry => entry.name === repository.name) || entry?.status === "needs_user" ||
        entry?.reason || entry?.question || entry?.candidates.length > 0 || entry?.refresh_state;
    });
    return h("div", { className: "card-stack" },
      ACTIVE.has(stage) && state.phase === "branch" &&
      h(CardBox, { title: "项目与分支操作" },
        h(Space, null, h(Spin, { size: "small" }), stage)),
      detailRepositories.length > 0 && h("div", { className: "repository-grid" }, detailRepositories.map(repository => {
        const entry = repositories.find(entry => entry.role === repository.role);
        const workspace = state.repos.find(entry => entry.name === repository.name);
        return h(CardBox, { className: "app-card repository-card", key: repository.name,
          title: `${repository.name}分支与工作区` },
          (entry?.reason || workspace) && h("p", { className: "muted" }, entry?.reason || "沿用当前会话已创建工作区的分支。"),
          entry?.question && h(Alert, { type: "warning", showIcon: true, message: entry.question }),
          entry?.candidates.length > 0 && h("p", { className: "muted" }, `AI 候选：${entry.candidates.join("、")}`),
          entry?.refresh_state && h("p", { className: "muted" }, { running: "正在刷新远程分支…", done: "已刷新远程分支", failed: "远程分支刷新失败，修复连接后可恢复当前阶段" }[entry.refresh_state]),
          workspace && h(Descriptions, { column: 1, size: "small", style: { marginTop: 18 }, items: [{ key: "commit", label: "起始提交", children: h(Typography.Text, { code: true }, workspace.baseline.slice(0, 12)) }, { key: "path", label: workspace.workspace_owned === false ? "复用工作目录" : "独立工作区", children: h(Typography.Text, { style: { fontSize: 12 }, copyable: true }, workspace.path) }, ...(workspace.preserved_paths?.length ? [{ key: "preserved", label: "本机保留改动", children: `${workspace.preserved_paths.length} 个文件，不自动修改或提交` }] : [])] }));
      })),
      (["资料已读取", "准备工作区失败"].includes(stage) || state.supplement_images?.length > 0 || transfers.length > 0) &&
        h(CardBox, { title: "补充需求与验收标准" },
          h("div", { onPaste: supplementEditable ? pasteImages : undefined },
            h(Input.TextArea, { value: draft.supplement, rows: 6, disabled: !supplementEditable, placeholder: "补充业务规则、验收标准，可在此粘贴截图或图片…", onChange: event => changeDraft({ supplement: event.target.value }) }),
            h("div", { className: "supplement-image-tools" },
              h("input", { type: "file", ref: imageInput, hidden: true, multiple: true, accept: ".png,.jpg,.jpeg,.gif,.webp,.bmp", onChange: event => {
                addImages(event.target.files); event.target.value = "";
              } }),
              h(Button, { disabled: !supplementEditable, onClick: () => imageInput.current.click() }, "上传图片"),
              h(Typography.Text, { type: "secondary" }, `支持多选、分批上传及粘贴图片，单张不超过 ${model.settings.max_attachment_mb} MB`))),
          h("div", { className: "attachment-grid" },
            (state.supplement_images || []).map(image => h("div", { className: "attachment", key: image.id },
              h(Image, { src: `/api/supplement-images?${new URLSearchParams({ ...viewAddress(state.id), image_id: image.id })}`, alt: image.name, preview: { mask: "查看图片" } }),
              h("div", { className: "attachment-name" }, image.name),
              h("div", { className: "supplement-image-actions" }, h(Tag, { color: "success" }, "已上传"),
                editable && h(Button, { size: "small", type: "text", danger: true, onClick: () => removeImage(image) }, "移除")))),
            transfers.map(task => h("div", { className: "attachment", key: task.uid },
              h("div", { className: "attachment-name" }, task.name),
              h("div", { className: "supplement-image-actions" }, task.status === "error" ? h(Tag, { color: "error" }, "上传失败") :
                h(Space, null, h(Spin, { size: "small" }), task.status === "waiting" ? "等待上传" : "上传中")),
              task.status === "error" && h(React.Fragment, null,
                h(Typography.Text, { type: "danger" }, task.error),
                h(Space, null,
                  h(Button, { size: "small", disabled: !supplementEditable, onClick: () => { task.status = "waiting"; task.error = ""; publishImageTransfers(); uploadImages(state.id); } }, "重试"),
                  h(Button, { size: "small", type: "text", danger: true, disabled: !supplementEditable, onClick: () => {
                    const queue = imageQueues.current[state.id]; queue.tasks = queue.tasks.filter(entry => entry.uid !== task.uid); publishImageTransfers();
                  } }, "移除")))))),
          transfers.some(task => task.status === "error") && h(Typography.Text, { type: "warning" }, "请重试或移除失败图片后再开始计划。"),
          ["资料已读取", "准备工作区失败"].includes(stage) && !state.codex && h(CodexSelection, {
            key: state.id, value: draft.codex, disabled: !editable, onReady: setSessionSelection,
            onChange: codex => changeDraft({ codex }) }),
          state.codex && h(Typography.Paragraph, { type: "secondary" }, `会话模型：${state.codex.model} · 推理强度：${EFFORT_NAMES[state.codex.reasoning_effort] || state.codex.reasoning_effort}（${state.codex.reasoning_effort}）`),
          !state.plan_started_at && h("div", { className: "action-bar" },
            h(Checkbox, { checked: draft.compile_check_enabled, disabled: !editable,
              onChange: event => changeDraft({ compile_check_enabled: event.target.checked }) }, "Review 前自动检查后端编译"),
            h(Typography.Text, { type: "secondary" }, "仅检查有改动的后端模块；前端继续 Review。需要下载依赖时等待人工选择。")),
          ["资料已读取", "准备工作区失败"].includes(stage) && h("div", { className: "data-foot" }, h("span", { className: "muted" }, "开始后固定模型与推理强度；已有改动保留且不提交"),
            h(Button, { type: "primary", size: "large", disabled: !editable || transfers.length > 0 || (!state.codex && !sessionSelection), onClick: startPlan }, "开始需求分析与计划"))));
  }

  function renderFeedbackImages(images, editable = false) {
    return h("div", { className: "attachment-grid" }, images.map(image => h("div", { className: "attachment", key: image.id },
      h(Image, { src: `/api/supplement-images?${new URLSearchParams({ session_id: state.id, purpose: "followup", image_id: image.id })}`,
        alt: image.name, preview: { mask: "查看图片" } }),
      h("div", { className: "attachment-name" }, image.name),
      editable && h("div", { className: "supplement-image-actions" }, h(Button, { size: "small", type: "text", danger: true,
        onClick: () => removeImage(image, "followup") }, "移除")))));
  }

  function renderFeedbackTransfers() {
    return h("div", { className: "attachment-grid" }, transfers.filter(task => task.purpose === "followup").map(task =>
      h("div", { className: "attachment", key: task.uid }, h("div", { className: "attachment-name" }, task.name),
        h("div", { className: "supplement-image-actions" }, task.status === "error" ? h(Tag, { color: "error" }, "上传失败") :
          h(Space, null, h(Spin, { size: "small" }), task.status === "waiting" ? "等待上传" : "上传中")),
        task.status === "error" && h(React.Fragment, null, h(Typography.Text, { type: "danger" }, task.error),
          h(Space, null, h(Button, { size: "small", disabled: !followupEditable, onClick: () => {
            task.status = "waiting"; task.error = ""; publishImageTransfers(); uploadImages(state.id);
          } }, "重试"), h(Button, { size: "small", type: "text", danger: true, disabled: !followupEditable, onClick: () => {
            const queue = imageQueues.current[state.id]; queue.tasks = queue.tasks.filter(entry => entry.uid !== task.uid); publishImageTransfers();
          } }, "移除"))))));
  }

  function renderPlan() {
    if (!model.selectedSessionId || !state || !selectionMatches) return h(CardBox, null, h(Empty, { description: "先选择工作项并等待读取完成，再开始需求分析与计划" }));
    if (!state.codex && !["资料已读取", "资料读取失败"].includes(stage)) return h(CardBox, { title: "确认历史会话的模型配置" },
      h(Typography.Paragraph, null, "该历史会话未保存模型与推理强度。请选择一次，保存后按原阶段继续。"),
      h(CodexSelection, { key: state.id, value: draft.codex, disabled: busy, onReady: setSessionSelection, onChange: codex => changeDraft({ codex }) }),
      h("div", { className: "action-bar" }, h(Button, { type: "primary", disabled: busy || !sessionSelection,
        onClick: () => act("configure_codex", sessionSelection) }, "保存会话模型配置"),
      h(Button, { disabled: stopped || (!ACTIVE.has(stage) && stage !== "等待人工回答"), onClick: () => act("pause") }, "暂停当前阶段")));
    if (state.phase === "branch" || (!state.repos.length && !state.followup)) return h("div", { className: "card-stack" }, renderBranches(),
      h(CardBox, { title: "流程操作" },
        state.codex && h(Typography.Paragraph, { type: "secondary" }, `模型：${state.codex.model} · 推理强度：${EFFORT_NAMES[state.codex.reasoning_effort] || state.codex.reasoning_effort}（${state.codex.reasoning_effort}）`),
        h(Space, null,
        h(Button, { disabled: stopped || !ACTIVE.has(stage), onClick: () => act("pause") }, "暂停当前阶段"),
        h(Button, { disabled: busy || !["已暂停", "已停止"].includes(stage), onClick: () => act("resume") }, "从暂停点继续"))));
    const questions = state.pending?.params?.questions || [];
    const continuing = state.phase === "complete";
    const feedbackAuthorized = state.approval_source === "followup";
    const canAnswer = (["等待人工回答", "等待计划补充", "等待计划确认"].includes(stage) || continuing) && !busy &&
      (state.pending?.method !== "workflow/ask_user" || state.pending.turn_completed === true);
    const reviewsByMessage = new Map(state.reviews.map(entry => [entry.history_index, entry]));
    const entries = state.history.map((entry, index) => ({ ...entry, index })).filter(entry => entry.role !== "控制器");
    return h("div", { className: "conversation-grid" },
      h("div", { className: "card-stack" },
        renderBranches(),
        renderCompileChecks(),
        h(CardBox, { title: "工作流会话", extra: stageTag(stage) },
          state.codex && h(Typography.Paragraph, { type: "secondary" }, `模型：${state.codex.model} · 推理强度：${EFFORT_NAMES[state.codex.reasoning_effort] || state.codex.reasoning_effort}（${state.codex.reasoning_effort}）`),
          h("div", { className: "conversation-feed", ref: feedRef }, entries.length === 0 && h(Empty, { description: "等待开始计划" }),
            entries.map(entry => h("div", { className: `chat-entry ${entry.role === "用户" ? "user" : entry.role === "系统" ? "system" : "assistant"}`, key: entry.index },
              h("div", { className: "chat-meta" }, h("strong", null, entry.role), h("span", null, timeLabel(entry.time)),
                entry.delivery_round > 1 && h(Tag, null, `第 ${entry.delivery_round} 轮交付`),
                typeof entry.review_enabled === "boolean" && h(Tag, null, `修改后自动 Review：${entry.review_enabled ? "开启" : "关闭"}`)),
              h("div", { className: `chat-body${reviewsByMessage.has(entry.index) ? " review-message" : ""}` },
                reviewsByMessage.has(entry.index) ? renderReview(reviewsByMessage.get(entry.index), entry.text) :
                  entry.phase === "review" && entry.role === "Codex" ?
                    stage === "审查中" ? h("span", { className: "muted" }, "正在校验审查结果…") : h("pre", { className: "review-original" }, entry.text) :
                    entry.role === "Codex" ? h(Markdown, { text: entry.text }) : entry.text,
                entry.images?.length > 0 && renderFeedbackImages(entry.images)))),
            !model.stream && ["准备工作区中", "计划中", "实施中", "修复中", "续聊处理中", "编译修复中"].includes(stage) &&
              h("div", { className: "stream-indicator", role: "status" }, h(Spin, { size: "small" }),
                stage === "准备工作区中" ? " 正在准备工作区…" : " 请求已发送，正在等待模型回复…"),
            state.phase === "review" && stage === "审查中" ? h("div", { className: "stream-indicator", role: "status" }, h(Spin, { size: "small" }), " 正在审查，结果校验完成后显示问题卡片…") :
              model.stream && h("div", { className: "chat-entry assistant" }, h("div", { className: "stream-indicator" }, "正在分析…"), h("div", { className: "chat-body" }, h(Markdown, { text: model.stream }))))),
        h(CardBox, { title: questions.length ? "需要你的回答" : continuing ? "后续对话" : "补充说明与回复" },
          questions.map(question => {
            const key = `${state.pending.id}:${question.id}:${question.question}`;
            const value = draft.answers[key] || {};
            function update(change) { changeDraft({ answers: { ...draft.answers, [key]: { ...value, ...change } } }); }
            return h("div", { className: "question", key }, h("div", { className: "question-title" }, question.question),
              question.options?.length > 0 && h(Radio.Group, { value: value.choice, onChange: event => update({ choice: event.target.value }), disabled: !canAnswer, className: "question-options" },
                question.options.map(option => h("div", { key: option.label }, h(Radio, { value: option.label }, option.label), option.description && h("div", { className: "option-note" }, option.description)))),
              h(Input.TextArea, { value: value.custom || "", rows: 2, disabled: !canAnswer, placeholder: "也可以填写自定义答案或补充说明", onChange: event => update({ custom: event.target.value }) }));
          }),
          h(Input.TextArea, { rows: 3, value: draft.reply, disabled: !canAnswer,
            onPaste: continuing && followupEditable ? event => pasteImages(event, "followup") : undefined,
            placeholder: questions.length ? "其他补充说明（可选）" : continuing ? "输入问题、需要获取的信息或修改要求，可粘贴图片…" : "输入回复、需要调整的计划内容或补充业务规则…", onChange: event => changeDraft({ reply: event.target.value }) }),
          continuing && h(React.Fragment, null,
            h("div", { className: "action-bar" },
              h(Switch, { checked: draft.review_enabled, disabled: !followupEditable, "aria-label": "修改后自动 Review", onChange: checked => changeDraft({ review_enabled: checked }) }),
              h(Typography.Text, null, "修改后自动 Review"),
              h(Typography.Text, { type: "secondary" }, draft.review_enabled ? "本轮有代码修改时审查，通过后自动本地提交" : "本轮不自动审查或提交，修改保留在工作区")),
            h("div", { className: "supplement-image-tools" },
              h("input", { type: "file", ref: followupImageInput, hidden: true, multiple: true, accept: ".png,.jpg,.jpeg,.gif,.webp,.bmp", onChange: event => {
                addImages(event.target.files, "followup"); event.target.value = "";
              } }),
              h(Button, { disabled: !followupEditable, onClick: () => followupImageInput.current.click() }, "上传图片"),
              h(Typography.Text, { type: "secondary" }, `单张不超过 ${model.settings.max_attachment_mb} MB；可用于提问、说明或反馈问题`)),
            renderFeedbackImages(state.followup_images.filter(image => image.history_index === undefined), followupEditable),
            renderFeedbackTransfers(),
            transfers.some(task => task.status === "error") && h(Typography.Text, { type: "warning" }, "请重试或移除失败图片后再发送。")),
          h("div", { className: "action-bar" }, h(Button, { type: "primary", disabled: !canAnswer || (continuing && transfers.length > 0), onClick: answer }, questions.length ? "提交回答并继续" : continuing ? "发送" : "提交补充说明")))),
      h("div", { className: "card-stack" }, h(CardBox, { title: feedbackAuthorized ? "原需求实施计划" : "实施计划", extra: state.plan && h(Tag, { color: state.approved_hash === state.plan_hash ? "success" : "gold" }, feedbackAuthorized ? "原计划 · 业务背景" : state.approved_hash === state.plan_hash ? (state.approval_source === "auto" ? "自动授权实施" : "人工确认实施") : "待确认") },
        state.plan ? h(React.Fragment, null, h("div", { className: "plan-panel" }, h(Markdown, { text: state.plan })), h("div", { className: "plan-version" }, `计划版本 ${state.plan_hash.slice(0, 12)}`)) : h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: "完成需求澄清后生成计划" }),
        h(Button, { type: "primary", block: true, disabled: stage !== "等待计划确认" || busy, onClick: () => act("approve_plan", { plan_hash: state.plan_hash }) }, "确认当前计划并实施")),
        h(CardBox, { title: "流程操作" }, h(Space, { direction: "vertical", style: { width: "100%" } },
          h(Button, { block: true, disabled: stopped || (!ACTIVE.has(stage) && stage !== "等待人工回答"), onClick: () => act("pause") }, "暂停当前阶段"),
          h(Button, { block: true, disabled: busy || !["已暂停", "已停止"].includes(stage), onClick: () => act("resume") }, "从暂停点继续"),
          h(Button, { block: true, disabled: !review, onClick: () => document.getElementById(`review-${review.number}`)?.scrollIntoView({ behavior: "smooth", block: "start" }) }, "定位最新 Review")),
          h("p", { className: "muted", style: { margin: "15px 0 0", lineHeight: 1.8 } }, continuing || state.followup ?
            "后续对话支持提问、获取信息和提出修改要求。每条消息的 Review 开关默认关闭；开启且有代码修改时审查，无遗留问题后自动本地 commit，不 push。需要澄清时等待回答，扩大范围时先重新计划。" :
            "未提问且一次成稿的计划自动实施；有提问或多回合补充时等待确认。Review 无遗留问题后自动本地 commit，不 push。")),
        Object.keys(state.commits || {}).length > 0 && h(CardBox, { title: "本地提交记录" }, h(Descriptions, { column: 1, items: Object.entries(state.commits).map(([name, sha]) => ({ key: name, label: name, children: h(Typography.Text, { copyable: true, code: true }, sha) })) })),
        state.deliveries.length > 0 && h(CardBox, { title: "历史交付记录" }, h(Collapse, { ghost: true, items: state.deliveries.map(delivery => ({
          key: delivery.number, label: `第 ${delivery.number} 轮交付 · ${timeLabel(delivery.completed_at)}${typeof delivery.review_enabled === "boolean" ? ` · 自动 Review ${delivery.review_enabled ? "开启" : "关闭"}` : ""}${delivery.stage ? ` · ${delivery.stage}` : ""}`,
          children: h(Descriptions, { column: 1, items: Object.entries(delivery.commits).map(([name, sha]) => ({ key: name, label: name,
            children: h(Typography.Text, { code: true, copyable: true }, sha) })) })
        })) }))));
  }

  function renderCompileChecks() {
    const checks = state.compile_checks || [];
    const latest = checks.at(-1);
    const statusNames = { pending: "等待检查", running: "正在检查", passed: "通过", skipped: "人工跳过",
      dependencies: "缺少依赖", blocked: "需要人工处理", code_error: "代码错误", excluded: "不再检查" };
    return h(CardBox, { title: "后端编译检查", extra: h(Tag, null, state.compile_check?.enabled ? "已开启" : "已关闭") },
      checks.length === 0 ? h(Typography.Text, { type: "secondary" }, state.compile_check?.enabled ? "实施或修复完成后，按模块检查有任务改动的后端；前端继续参与 Review。" : "本会话不自动执行后端编译检查。") :
        h(Collapse, { defaultActiveKey: [latest.id], key: latest.id, ghost: true, items: checks.map((batch, index) => ({
          key: batch.id, label: `第 ${index + 1} 次检查 · 第 ${batch.delivery_round} 轮交付 · 自动修复 ${batch.fix_round}/${batch.limit}`,
          children: h(Space, { direction: "vertical", style: { width: "100%" } }, batch.repositories.length === 0 && "没有需要编译的后端任务改动，前端仍按原流程参与 Review。",
            batch.repositories.map(entry => {
              const current = state.repos.some(repo => repo.name === entry.name && repo.role === "backend") &&
                batch.id === latest.id && batch.delivery_round === state.delivery_round && state.phase === "compile" &&
                ["等待编译选择", "已暂停", "已停止"].includes(stage) && !["passed", "skipped", "pending", "excluded"].includes(entry.status);
              const retired = state.repos.some(repo => repo.name === entry.name && repo.role === "frontend") &&
                batch.id === latest.id && batch.delivery_round === state.delivery_round && state.phase === "compile" &&
                ["等待编译选择", "已暂停", "已停止"].includes(stage) && !["passed", "skipped", "excluded"].includes(entry.status);
              const choose = decision => act("compile_decision", { batch_id: batch.id, repository: entry.name, decision });
              return h("div", { className: "question", key: entry.name },
                h(Space, null, h("strong", null, entry.name), h(Tag, { color: entry.status === "passed" ? "success" : entry.status === "skipped" ? "default" : "gold" }, statusNames[entry.status] || entry.status)),
                h("p", null, entry.summary),
                entry.modules.length > 0 && h(Typography.Paragraph, { type: "secondary" }, `模块：${entry.modules.join("、")}（含所需模块）`),
                entry.diagnostics?.map((diagnostic, number) => h("p", { key: number }, `${diagnostic.path}:${diagnostic.line} · ${diagnostic.message}`)),
                retired && h(Button, { disabled: busy, onClick: () => choose("retry") }, "前端检查已取消，继续后端流程"),
                current && h(Space, { wrap: true },
                  entry.status === "dependencies" && h(Button, { disabled: busy, onClick: () => choose("download") }, "允许下载并继续"),
                  h(Button, { disabled: busy, onClick: () => choose("retry") }, "重新检查"),
                  h(Button, { disabled: busy, onClick: () => choose("skip") }, "跳过该仓库本轮检查"),
                  entry.status === "code_error" && batch.fix_round >= batch.limit && h(Button, { disabled: busy, onClick: () => choose("add_round") }, "追加一轮编译修复"),
                  h(Button, { disabled: busy, onClick: () => choose("pause") }, "暂停")));
            })),
        })) }));
  }

  function renderReview(entry, originalText) {
    const latest = entry.number === review?.number && (entry.delivery_round || 1) === state.delivery_round && state.phase !== "complete";
    const findings = entry.result.findings;
    const selected = latest ? draft.selected_findings.filter(id => findings.some(finding => finding.id === id)) : [];
    const limitReached = state.fix_round >= state.max_fix_rounds;
    const canSelect = latest && !busy && ["等待修复选择", "部分提交"].includes(stage);
    const canFix = latest && stage === "等待修复选择" && !busy;
    const canCommit = canSelect && selected.length === findings.length && (!findings.length || draft.review_note.trim());
    return h("section", { className: "review-content", id: `review-${entry.number}`, "aria-label": `第 ${entry.number} 次 Review` },
      h("div", { className: "review-heading" }, h("strong", null, `第 ${entry.number} 次 Review`),
        h(Tag, null, latest ? `修复 ${state.fix_round} / ${state.max_fix_rounds} 轮` : `修复第 ${entry.fix_round} 轮后`), !latest && h(Tag, null, "历史记录 · 只读")),
      h("div", { className: "review-summary" }, h("div", { className: "review-number" }, findings.length, h("span", null, "待处置问题")), h("div", { className: "review-copy" }, h(Markdown, { text: entry.result.summary }))),
      h("div", { className: "finding-list" }, findings.length === 0 && h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: "本轮没有发现待处理问题" }),
        findings.map(finding => h("div", { className: `finding-card${selected.includes(finding.id) ? " selected" : ""}`, key: finding.id },
          h("div", { className: "finding-title" }, h(Checkbox, { checked: selected.includes(finding.id), disabled: !canSelect,
            onChange: event => changeDraft({ selected_findings: event.target.checked ? [...selected, finding.id] : selected.filter(id => id !== finding.id) }) },
            h(Tag, { color: ["P0", "P1"].includes(finding.severity) ? "error" : finding.severity === "P2" ? "warning" : "default" }, finding.severity),
            h("strong", null, finding.title))),
          h("div", { className: "finding-meta" }, h(Typography.Text, { code: true }, finding.id), h(Tag, null, finding.category),
            h(Tag, { color: finding.origin === "本次引入" ? "volcano" : finding.origin === "证据不足" ? "gold" : "default" }, finding.origin)),
          h("div", { className: "finding-location" }, `${finding.repository}/${finding.path}:${finding.line}`),
          h(Collapse, { ghost: true, size: "small", items: [{ key: "details", label: "判断依据、处理建议与业务影响",
            children: h("div", { className: "finding-details" }, ...[["判断依据", finding.evidence], ["处理建议", finding.suggestion], ["业务影响", finding.impact]].map(([label, value]) => h("div", { key: label }, h("strong", null, label), h(Markdown, { text: value })))) }] })))),
      latest && h("div", { className: "review-actions" },
        h(Button, { onClick: openDiff, disabled: stopped }, "查看累计代码差异"),
        limitReached && findings.length > 0 && h(Alert, { type: "warning", showIcon: true, style: { marginTop: 14 }, message: "已达到本次修复轮次上限", description: "如需继续修复，请明确追加一轮；仍有问题时不会自动结束。" }),
        h(Field, { label: "修复说明 / 接受剩余问题的原因" }, h(Input.TextArea, { rows: 3, value: draft.review_note, disabled: !canSelect,
          placeholder: "补充修复要求。接受剩余问题并结束时，请填写接受原因。", onChange: event => changeDraft({ review_note: event.target.value }) })),
        h("div", { className: "action-bar" },
          h(Button, { disabled: !canFix, onClick: () => act("add_round") }, "追加一轮"),
          h(Button, { disabled: busy || !["等待修复选择", "已暂停", "已停止"].includes(stage), onClick: () => act("rereview") }, "重新 Review"),
          h(Button, { type: "primary", disabled: !canFix || !selected.length || limitReached, onClick: () => act("fix", { ids: selected, note: draft.review_note }) }, `修复选中 ${selected.length} 项并复审`)),
        h(Divider), h("div", { className: "review-finalize" },
          h("span", { className: "muted" }, findings.length ? "结束前须选中全部剩余问题，并填写接受原因" : state.phase === "complete" ? "本地提交已完成，未 push" : "无遗留问题时自动本地提交；提交失败后可手动继续"),
          h(Button, { disabled: !canCommit, onClick: () => act("finalize", { ids: selected, note: draft.review_note }) }, findings.length ? "接受剩余问题并本地提交" : "继续本地提交"))),
      h(Collapse, { ghost: true, size: "small", items: [{ key: "original", label: "输出原文", children: h("pre", { className: "review-original" }, originalText) }] }));
  }

  function renderSettings() {
    const editable = model.history.every(entry => !entry.working && !entry.deleting && (COMPLETE.has(entry.stage) || ["资料已读取", "资料读取失败"].includes(entry.stage)));
    return h(CardBox, { title: "默认配置" }, !editable && h(Alert, { type: "info", showIcon: true, message: "有会话的流程配置已固定，请结束这些流程后再修改", style: { marginBottom: 20 } }),
      h(Form, { form: settingsForm, layout: "vertical", disabled: !editable || busy, onFinish: async values => { if (await act("settings", values)) { settingsForm.setFieldValue("password", ""); notice.success({ message: "设置已提交", description: "保存后请重新连接禅道。" }); } } },
        h("div", { className: "settings-grid" },
          h(Form.Item, { name: "base_url", label: "禅道地址", rules: [{ required: true, message: "请填写禅道地址" }] }, h(Input, { autoComplete: "off" })),
          h(Form.Item, { name: "account", label: "登录账号", rules: [{ required: true, message: "请填写账号" }] }, h(Input, { autoComplete: "off" })),
          h(Form.Item, { name: "password", label: "登录密码", extra: "留空保持已保存值；填写后使用本机加密保存" }, h(Input.Password, { autoComplete: "new-password", placeholder: "不回显已保存的密码" })),
          h(Form.Item, { name: "max_fix_rounds", label: "默认修复轮次上限", rules: [{ required: true, message: "请填写轮次" }] }, h(InputNumber, { min: 0, precision: 0, className: "full-width" }))),
        h(Form.Item, { name: "compile_check_enabled", valuePropName: "checked", extra: "仅检查后端改动模块及所需模块；前端继续 Review。新会话可单独调整。" },
          h(Checkbox, null, "Review 前自动检查后端编译")),
        h(Form.Item, { name: "model", hidden: true }, h(Input)),
        h(Form.Item, { name: "reasoning_effort", hidden: true }, h(Input)),
        h(Form.Item, { label: "新会话默认模型", extra: "开始会话前可单独调整，已开始的会话保持原配置。" },
          h(CodexSelection, { value: { model: settingsModel ?? "", reasoning_effort: settingsEffort ?? "" }, disabled: !editable || busy,
            onReady: setSettingsSelection, onChange: values => settingsForm.setFieldsValue(values) })),
        h(Form.Item, { name: "review_prompt", label: "固定 Review 提示词", rules: [{ required: true, message: "请填写固定 Review 提示词" }] }, h(Input.TextArea, { rows: 5 })),
        h(Descriptions, { size: "small", column: 1, items: [{ key: "feishu", label: "飞书文档", children: "第一版暂不读取" }, ...model.settings.repositories.map(repository => ({ key: repository.name, label: `${repository.name}仓库`, children: repository.path }))] }),
        h("div", { className: "action-bar" }, h(Button, { type: "primary", htmlType: "submit", disabled: !settingsSelection, loading: model.busy === "settings" }, "保存设置"))),
      h(Divider), h("div", { className: "data-foot", style: { border: 0, paddingTop: 0 } }, h("span", { className: "muted" }, "停止服务会保存记录，并暂停正在执行的阶段。"),
        h(Button, { danger: true, disabled: stopped || sending === "shutdown", onClick: stopService }, "暂停并停止本地服务")));
  }

  function renderSessions() {
    const stages = [...new Set(model.history.map(entry => entry.stage))];
    const projects = projectCatalog(model);
    const query = sessionQuery.trim().toLowerCase();
    const groups = [...projects, { id: 0, name: "无项目对话" }].map(project => {
      const entries = model.history.filter(entry => (entry.project?.id || 0) === project.id);
      const rows = entries.filter(entry => (sessionFilter === "all" || entry.stage === sessionFilter) &&
        (!query || `${entry.title} ${itemLabel(entry)} ${project.name} ${project.id > 0 ? `项目 #${project.id}` : ""}`.toLowerCase().includes(query)))
        .sort((left, right) => right.updated_at.localeCompare(left.updated_at) || right.id.localeCompare(left.id));
      return { ...project, rows, updated_at: entries.reduce((latest, entry) => entry.updated_at > latest ? entry.updated_at : latest, "") };
    }).filter(project => !project.id || project.manual || (!query && sessionFilter === "all") || project.rows.length)
      .sort((left, right) => !left.id ? 1 : !right.id ? -1 : right.updated_at.localeCompare(left.updated_at) || left.id - right.id);
    const running = model.history.filter(entry => ACTIVE.has(entry.stage) || entry.busy).length;
    return h(CardBox, { title: "会话", className: "app-card session-panel", extra: h(Typography.Text, { type: "secondary" }, `${running} 个执行中`) },
      h(Button, { type: "primary", block: true, onClick: newSession, disabled: stopped }, "新建会话"),
      h(Button, { block: true, className: "add-project-button", onClick: addProjectPartition, disabled: stopped || !model.connected }, "添加项目分区"),
      h(Input.Search, { value: sessionQuery, allowClear: true, placeholder: "搜索会话或项目名称、编号", onChange: event => setSessionQuery(event.target.value) }),
      h(Select, { value: sessionFilter, className: "full-width", onChange: setSessionFilter,
        options: [{ value: "all", label: "全部状态" }, ...stages.map(value => ({ value, label: value }))] }),
      h("div", { className: "session-list" }, groups.map(project => {
        const key = String(project.id);
        return h("section", { key, "data-project-id": key, className: `session-project${dropProjectId === key ? " drop-target" : ""}`,
          onDragOver: event => {
            if (!dragSessionId || !model.history.find(entry => entry.id === dragSessionId)?.can_move_project) return;
            event.preventDefault(); event.dataTransfer.dropEffect = "move"; setDropProjectId(key);
          },
          onDragLeave: event => { if (!event.currentTarget.contains(event.relatedTarget)) setDropProjectId(null); },
          onDrop: event => {
            if (!dragSessionId) return;
            event.preventDefault();
            const entry = model.history.find(value => value.id === dragSessionId);
            if (entry?.can_move_project && (entry.project?.id || 0) !== project.id) moveProject(entry.id, project.id || null);
            setDropProjectId(null); setDragSessionId(null);
          } },
          h(Collapse, { ghost: true, activeKey: query || !collapsedProjects.includes(key) ? [key] : [],
            onChange: keys => { if (!query) changeProjectCollapse(keys.length ? collapsedProjects.filter(value => value !== key) : [...collapsedProjects, key]); },
            items: [{ key, label: h("div", { className: "session-project-heading" },
              h("span", { className: "session-project-name", title: project.id ? `${project.name || "项目"} #${project.id}` : project.name }, project.name || `项目 #${project.id}`,
                project.id > 0 && h("span", { className: "session-project-number" }, ` #${project.id}`)),
              h(Tag, { className: "session-project-count" }, project.rows.length)),
              children: h(List, { dataSource: project.rows, locale: { emptyText: "暂无会话，可移入未启动的会话" }, renderItem: entry => {
                const selected = model.selectedSessionId === entry.id;
                const canMove = entry.can_move_project && !stopped && !requestsPending[entry.id];
                const cannotDelete = stopped || entry.working || entry.busy || ACTIVE.has(entry.stage) || entry.stage === "等待人工回答" || entry.stage === "资料读取中";
                const open = () => selectSession(entry.id, ["资料已读取", "资料读取失败"].includes(entry.stage) ? "items" : "plan");
                return h(List.Item, { key: entry.id, className: `session-entry${selected ? " selected" : ""}`, onClick: open, draggable: Boolean(canMove),
                  title: entry.project_move_reason || "可拖动到其他项目分区",
                  onDragStart: event => { event.dataTransfer.setData("application/x-workflow-session", entry.id); event.dataTransfer.effectAllowed = "move"; setDragSessionId(entry.id); },
                  onDragEnd: () => { setDragSessionId(null); setDropProjectId(null); },
                  tabIndex: 0, role: "button", "aria-pressed": selected, onKeyDown: event => { if (event.target === event.currentTarget && ["Enter", " "].includes(event.key)) { event.preventDefault(); open(); } } },
                  h("div", { className: "session-entry-body" },
                    h("div", { className: "session-entry-custom-title", title: entry.title }, entry.title),
                    h("div", { className: "session-entry-title", title: itemLabel(entry) }, sessionItems(entry).map(item => h("div", { key: itemKey(item) }, `${TYPE_NAMES[item.kind]} #${item.id} · ${item.title}`))),
                    h("div", { className: "session-entry-status" }, stageTag(entry.stage), entry.busy && h(Spin, { size: "small" })),
                    h("div", { className: "session-entry-foot" }, h("span", { className: "muted" }, timeLabel(entry.updated_at)),
                      h("span", { className: "session-entry-actions", onClick: event => event.stopPropagation() },
                      h(Button, { type: "text", size: "small", disabled: stopped || entry.deleting || Boolean(requestsPending[entry.id]),
                        "aria-label": "修改会话标题", onClick: () => renameSession(entry) }, "改名"),
                      h(Tooltip, { title: entry.project_move_reason || "更换本次工作流的执行项目" }, h("span", null,
                        h(Dropdown, { disabled: !canMove, trigger: ["click"], menu: { selectedKeys: [String(entry.project?.id || 0)],
                          items: [...projects.map(value => ({ key: String(value.id), label: `${value.name || "项目"} #${value.id}`, disabled: value.id === entry.project?.id })),
                            { key: "0", label: "无项目对话", disabled: !entry.project?.id }],
                          onClick: ({ key }) => moveProject(entry.id, key === "0" ? null : Number(key)) } },
                          h(Button, { type: "text", size: "small", disabled: !canMove, "aria-label": "移动到项目" }, "移动")))),
                      h(Button, { type: "link", danger: true, size: "small", disabled: Boolean(cannotDelete), title: cannotDelete ? "先暂停并等待操作结束后再删除" : "删除会话及工作区",
                        onClick: event => { event.stopPropagation(); deleteSession(entry.id); } }, "删除")))));
              } }) }] }));
      })), h("div", { className: "muted session-panel-note" }, "未启动的会话可拖动或通过“移动”更换项目。切换查看不会停止后台会话。"));
  }

  if (fatal) return h("div", { style: { maxWidth: 650, margin: "100px auto" } }, h(Alert, { type: "error", showIcon: true, message: "无法连接本地网页服务", description: fatal, action: h(Button, { onClick: () => location.reload() }, "重新打开") }));
  if (!model) return h("div", { className: "initial-loading" }, h(Spin, null), " 正在加载工作流…");
  const heading = PAGES[page];
  const renderers = { items: renderItems, plan: renderPlan, settings: renderSettings };
  const menuIcons = ["▦", "☷", "⚙"];
  const hasProjectInfo = page !== "settings" && selectionMatches;
  const showProjectInfo = hasProjectInfo && !projectInfoHidden;
  return h(ConfigProvider, { locale: antd.locales.zh_CN, theme: { token: { colorPrimary: "rgb(27, 143, 255)", borderRadius: 6, fontSize: 14,
    fontFamily: '-apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif' }, components: { Button: { colorLink: "rgb(27, 143, 255)", defaultColor: "rgb(27, 143, 255)", defaultHoverColor: "rgb(27, 143, 255)" } } } },
    noticeHolder, modalHolder,
    h(Layout, { className: `app-layout${navCollapsed ? " nav-collapsed" : ""}` },
      h(Layout.Sider, { width: 214, collapsedWidth: 64, collapsible: true, collapsed: navCollapsed, trigger: null, theme: "light", className: "app-sider" },
        h("div", { className: "brand", title: navCollapsed ? "研发工作流" : undefined }, h("div", { className: "brand-mark" }, "工"), !navCollapsed && h("div", null, h("div", { className: "brand-title" }, "研发工作流"), h("div", { className: "brand-subtitle" }, "禅道 · CODEX"))),
        h("div", { className: "navigation-toggle" }, h(Button, { type: "text", block: true, title: navCollapsed ? "展开导航" : "收起导航",
          "aria-label": navCollapsed ? "展开导航" : "收起导航", "aria-expanded": !navCollapsed, "aria-controls": "main-navigation", onClick: () => setNavCollapsed(value => !value),
          icon: h("span", { className: "nav-icon", "aria-hidden": true }, navCollapsed ? "›" : "‹") }, !navCollapsed && "收起导航")),
        h(Menu, { id: "main-navigation", className: "navigation", mode: "inline", selectedKeys: [page], onClick: value => navigate(value.key), items: Object.entries(PAGES).map(([key, values], index) => ({ key, icon: h("span", { className: "nav-icon", "aria-hidden": true }, menuIcons[index]), label: values[0] })) }),
        h("div", { className: "sider-foot" }, !navCollapsed && h("strong", null, model.settings.account),
          h(Tooltip, { title: navCollapsed ? `${model.settings.account} · ${online ? "本地服务已连接" : "连接恢复中"}` : undefined, placement: "right" },
            h("span", { "aria-label": online ? "本地服务已连接" : "连接恢复中" }, h(Badge, { status: online ? "success" : "default", text: !navCollapsed && h("span", { className: "muted" }, online ? "本地服务已连接" : "连接恢复中") }))))),
      h(Layout, { className: "main-layout" },
        h(Layout.Header, { className: "app-header" }, h("div", { className: "header-section" }, h("span", { className: "header-caption" }, "工作台"), h("span", null, heading[0])),
          h(Space, { size: 16 }, h(Badge, { status: model.connected ? "success" : "default", text: model.connected ? `禅道已连接 · ${model.settings.account}` : "禅道未连接" }),
            h(Button, { onClick: () => act("connect"), disabled: busy, loading: model.busy === "connect" }, model.connected ? "重新连接" : "连接禅道"))),
        h(Layout.Content, { className: "page-content" },
          !online && h(Alert, { className: "offline-banner", type: stopped ? "info" : "warning", showIcon: true, message: stopped ? "本地服务已停止" : "与本地服务的实时连接暂时中断", description: stopped ? "会话记录已保留。需要继续时，重新运行启动脚本后刷新页面。" : "页面会尝试恢复事件连接；如果服务已重启，请刷新页面。", action: h(Button, { onClick: () => location.reload() }, "刷新页面") }),
          state?.error && stage !== "资料读取失败" && h(Alert, { type: "error", showIcon: true, message: "当前流程需要处理", description: state.error, style: { marginBottom: 16 } }),
          h("div", { className: "page-heading" }, h("div", null, h("h1", null, heading[0]), h("div", { className: "page-subtitle" }, heading[1]))),
          h("div", { className: page === "settings" ? "" : "session-workspace" }, page !== "settings" && renderSessions(), h("div", { className: "session-content" },
          page !== "settings" && h(Card, { className: "workflow-strip", size: "small" }, h("div", { className: "workflow-strip-top" }, h("div", { className: "current-item", title: state ? itemLabel(state) : "" }, state ? itemLabel(state) : "选择工作项，开始一次完整的需求交付"),
            h("div", { className: "workflow-strip-actions" }, stageTag(stage), hasProjectInfo && projectInfoHidden &&
              h(Button, { size: "small", "aria-controls": "project-info", "aria-expanded": false, onClick: () => {
                setProjectInfoHidden(false); sessionStorage.setItem("workflow-project-info-hidden", "false");
              } }, "项目信息"))),
            h(Steps, { current: stepOf(state), status: stage === "已完成（未审查）" ? "finish" : undefined, size: "small", responsive: false,
              items: ["选择工作项", "制定计划", state?.followup ? "处理消息" : "实施修改", "Review", "本地提交"].map((title, index) =>
                stage === "已完成（未审查）" && index > 2 ? { title: `${title}（跳过）`, status: "wait" } : { title }) })),
          h("div", { className: `session-body${showProjectInfo ? " with-project-info" : ""}` },
            h("div", { className: "workspace-main" }, renderers[page]()), showProjectInfo && renderProjectInfo()))), h("div", { className: "page-footer" }, "数据保存在本机 · 关闭页面不会取消已确认的后台任务")))) ,
    h(Drawer, { title: "相对初始基线的累计代码差异", placement: "right", width: "80vw", open: diffOpen, onClose: () => setDiffOpen(false) },
      diffs === null ? h(Spin, null) : h(Collapse, { defaultActiveKey: diffs.map((_, index) => String(index)), items: diffs.map((diff, index) => ({ key: String(index), label: diff.repository,
        children: diff.commit ? h(Typography.Text, { copyable: true }, diff.commit) : h(React.Fragment, null, h("pre", { className: "diff-text" }, diff.stat || "没有已跟踪文件差异"), diff.patch && h("pre", { className: "diff-text" }, diff.patch),
          diff.new_files.map(file => h("div", { key: file.path }, h("strong", null, file.path), h("pre", { className: "diff-text" }, file.patch || file.note)))) })) })));
}

ReactDOM.createRoot(document.getElementById("root")).render(h(WorkflowApp));
