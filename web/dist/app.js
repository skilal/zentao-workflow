"use strict";

const { useState, useEffect, useLayoutEffect, useRef } = React;
const { Layout, Menu, Card, Button, Space, Tag, Table, Input, Select, Radio, Checkbox,
  Typography, Empty, Alert, Steps, Badge, Image, Form, InputNumber, ConfigProvider,
  Drawer, Collapse, Spin, notification, Divider, Descriptions, List, Modal, Tooltip, Dropdown, Switch, Tabs, Grid } = antd;
const h = React.createElement;
DOMPurify.addHook("afterSanitizeAttributes", node => {
  if (node.nodeName === "INPUT") {
    if (node.getAttribute("type") !== "checkbox") node.remove();
    else node.setAttribute("disabled", "");
  }
});
const TYPE_NAMES = { epic: "业务需求", requirement: "用户需求", story: "研发需求", task: "任务", bug: "Bug" };
const STATUS_NAMES = { wait: "未开始", doing: "进行中", pause: "已暂停", done: "已完成", closed: "已关闭", cancel: "已取消", active: "已激活", draft: "草稿", reviewing: "评审中", changing: "变更中", resolved: "已解决", confirmed: "已确认", postponed: "已延期", delay: "已逾期" };
const ACTIVE = new Set(["识别分支中", "刷新分支中", "拉取分支中", "准备工作区中", "计划中", "实施中", "审查中", "修复中", "续聊处理中", "编译检查中", "编译修复中", "编译分析中"]);
const COMPLETE = new Set(["已完成（静态审查）", "已完成（未审查）"]);
const PAGES = {
  items: ["工作项与资料", "可从指派给你的需求、任务和 Bug 中选择处理对象，也可新建会话直接输入需求。"],
  plan: ["计划与会话", "每轮计划未提问且一次完整生成时自动实施；需要决策时等待你的答复。"],
  prompts: ["内置提示词", "查看各阶段提示词原文、组装模板和生效条件。"],
  settings: ["工作流设置", "管理禅道账号、后端编译检查、固定 Review 提示词和默认修复轮次。"]
};

function itemKey(item) { return item ? `${item.kind}:${item.id}` : ""; }
function sessionItems(state) { return state.items || [state.item]; }
function itemLabel(state) { return sessionItems(state).map(item => `${TYPE_NAMES[item.kind]} #${item.id} · ${item.title}`).join("；") || state.title || "新会话"; }
function timeLabel(value) { return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : ""; }
function itemProjectLabel(item) { return item.project_name || (item.project_id > 0 ? `项目 #${item.project_id}` : "待确认项目"); }
function dialogueTime(entry) { return entry.last_dialogue_at ? Date.parse(entry.last_dialogue_at) : -Infinity; }
function compareSessions(left, right) {
  const leftTime = dialogueTime(left), rightTime = dialogueTime(right);
  return leftTime === rightTime ? right.id.localeCompare(left.id) : rightTime - leftTime;
}
function plainDescription(value) {
  return String(value || "").replaceAll("【spec】", "【需求描述】").replaceAll("【verify】", "【验收标准】")
    .replaceAll("【desc】", "【详细描述】").replaceAll("【steps】", "【复现步骤】").replaceAll("【bugSteps】", "【来源 Bug 描述】");
}
function typeTag(kind) { return h(Tag, { color: { epic: "purple", requirement: "geekblue", story: "blue", task: "cyan", bug: "orange" }[kind] }, TYPE_NAMES[kind]); }
function stageTag(stage) {
  const color = stage?.includes("等待") ? "gold" : stage?.includes("完成") ? "success" : stage?.includes("停止") || stage?.includes("失败") ? "error" : ACTIVE.has(stage) ? "processing" : "default";
  return h(Tag, { color }, stage || "尚未选择工作项");
}
function stepOf(state) {
  if (!state) return 0;
  if (state.phase === "complete") return state.stage === "已完成（未审查）" ? 2 : 4;
  if (state.phase === "plan") return state.repos?.length ? 1 : 0;
  if (["followup", "compile", "compile_fix", "compile_analysis"].includes(state.phase)) return 2;
  if (state.phase === "review" || state.phase === "fix") return 3;
  if (state.phase === "implement") return 2;
  if (state.repos?.length) return 1;
  return 0;
}
function CardBox(props) { return h(Card, { className: "app-card", ...props }, props.children); }
function DetailSection({ title, summary, children, defaultOpen = false, expansionKey = "", className = "" }) {
  const [expanded, setExpanded] = useState(defaultOpen);
  useEffect(() => { setExpanded(defaultOpen); }, [expansionKey, defaultOpen]);
  return h(Collapse, { className: `app-card detail-section ${className}`, size: "small",
    activeKey: expanded ? ["details"] : [], onChange: keys => setExpanded(keys.includes("details")),
    items: [{ key: "details", label: h("div", { className: "detail-section-heading" },
      h("span", { className: "detail-section-title" }, title),
      summary && h("div", { className: "detail-section-summary" }, summary)), children }] });
}
const Markdown = React.memo(function Markdown({ text }) {
  const source = String(text || "").replace(/<\/?proposed_plan>/g, "");
  const html = DOMPurify.sanitize(marked.parse(source, { gfm: true, breaks: false }), {
    ALLOWED_TAGS: ["h1", "h2", "h3", "h4", "h5", "h6", "p", "br", "hr", "strong", "em", "del", "ul", "ol", "li", "blockquote", "a", "pre", "code", "table", "thead", "tbody", "tr", "th", "td", "input"],
    ALLOWED_ATTR: ["href", "title", "class", "start", "align", "type", "checked", "disabled"],
    ALLOW_DATA_ATTR: false
  });
  return h("div", { className: "markdown-body", dangerouslySetInnerHTML: { __html: html } });
});
const PROCESS_STATUS = { running: "进行中", completed: "已完成", failed: "失败", interrupted: "已暂停", disconnected: "连接已断开", waiting: "等待你的回答" };
function updateProgress(turns, value) {
  if (value.op === "turn") return turns.some(turn => turn.id === value.turn.id) ? turns : [...turns, value.turn];
  return turns.map(turn => {
    if (turn.id !== value.turn_id) return turn;
    if (value.op === "status") return { ...turn, status: value.status, items: turn.items.map(item => ["running", "waiting"].includes(item.status) ?
      { ...item, status: ["running", "waiting", "completed"].includes(value.status) ? value.status : "interrupted" } : item) };
    if (value.op === "item") return { ...turn, items: turn.items.some(item => item.id === value.item.id) ?
      turn.items.map(item => item.id === value.item.id ? value.item : item) : [...turn.items, value.item] };
    return { ...turn, items: turn.items.map(item => {
      if (item.id !== value.item_id) return item;
      if (value.summary_index === null) return { ...item, text: item.text + value.delta };
      const summary = [...item.summary];
      while (summary.length <= value.summary_index) summary.push("");
      summary[value.summary_index] += value.delta;
      return { ...item, summary };
    }) };
  });
}
function ProgressTurn({ turn }) {
  const [expanded, setExpanded] = useState(null);
  const active = turn.status === "running";
  const messages = turn.items.filter(item => item.type === "agentMessage");
  const unfinished = !["running", "completed"].includes(turn.status);
  const visible = turn.items.filter(item => item.type !== "plan" && (item.type !== "agentMessage" ||
    (turn.phase !== "review" && (unfinished || (item.phase !== "final_answer" && (item.phase === "commentary" || item.id !== messages.at(-1)?.id))))));
  const activities = [...new Set(turn.items.filter(item => item.status === "running").map(item => item.label))];
  const label = active ? activities.join("、") || "模型正在处理中" : PROCESS_STATUS[turn.status];
  return h("div", { className: "process-turn", "data-turn-id": turn.id }, h(Collapse, {
    size: "small", activeKey: expanded ?? (active ? ["process"] : []), onChange: setExpanded,
    items: [{ key: "process", label: h(Space, { size: "small", wrap: true }, "处理过程",
      active && h(Spin, { size: "small" }), h("span", { role: "status" }, label)),
      children: visible.length ? visible.map(item => h("div", { className: "process-item", key: item.id, "data-item-id": item.id },
        h(Space, { size: "small", wrap: true }, h(Typography.Text, { strong: true }, item.label),
          h(Tag, { color: item.status === "failed" ? "error" : item.status === "running" ? "processing" : "default" }, PROCESS_STATUS[item.status])),
        item.paths.map(path => h("div", { className: "repo-path", key: path }, path)),
        item.type === "reasoning" ? item.summary.map((text, index) => text && h(Markdown, { key: index, text })) :
          item.type === "agentMessage" && item.text && h(Markdown, { text: item.text }))) :
        h(Typography.Text, { type: "secondary" }, active ? "模型正在处理中，等待可展示的过程内容。" : "本回合没有返回可展示的过程内容。") }]
  }));
}
function ChangedFiles({ sessionId, revision, running }) {
  const [files, setFiles] = useState(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let disposed = false;
    let timer;
    async function refresh() {
      try {
        const result = await fetch(`/api/changed-files?session_id=${encodeURIComponent(sessionId)}`,
          { signal: controller.signal, cache: "no-store" }).then(readResponse);
        if (!disposed) {
          setFiles(result);
          setFailed(false);
        }
      } catch (error) {
        if (!disposed) setFailed(true);
      } finally {
        if (!disposed && running) timer = setTimeout(refresh, 3000);
      }
    }
    refresh();
    return () => { disposed = true; controller.abort(); clearTimeout(timer); };
  }, [sessionId, revision, running]);
  return h(Collapse, { className: "changed-files", size: "small", defaultActiveKey: ["files"],
    items: [{ key: "files", label: `改动文件${files && !failed ? ` · ${files.total}` : ""}`,
      children: failed ? h(Typography.Text, { type: "secondary", role: "status" }, "文件清单读取失败") :
        files === null ? h(Typography.Text, { type: "secondary", role: "status" }, "正在读取文件清单…") :
          files.total === 0 ? h(Typography.Text, { type: "secondary" }, "暂无改动文件") :
            h("div", { className: "changed-files-list" }, [...files.repositories]
              .sort((left, right) => Number(right.name === "后端") - Number(left.name === "后端")).map(repo =>
              h("div", { className: "changed-files-repository", key: repo.name,
                style: { gridColumn: { "后端": 1, "前端": 2 }[repo.name] } },
                h(Typography.Text, { strong: true }, repo.name),
                repo.paths.map(path => h("div", { className: "repo-path", key: path }, path))))) }]
  });
}
function ChangeSummary({ pages = [] }) {
  return h(Collapse, { className: "change-locations", size: "small", defaultActiveKey: ["locations"],
    items: [{ key: "locations", label: "整体改动说明",
      children: pages.length === 0 ? h(Typography.Text, { type: "secondary" }, "暂无整体改动说明") :
        h("div", { className: "location-items" }, pages.map((item, index) =>
          h("div", { className: "location-item", key: index },
            h(Typography.Text, { strong: true }, item.page), h("div", null, item.description)))) }] });
}
function ItemsSyncStatus({ sync }) {
  const [elapsed, setElapsed] = useState(0);
  const loading = sync.status === "loading";
  useEffect(() => {
    if (!loading) return;
    const update = () => setElapsed(Math.max(0, Math.floor((Date.now() - Date.parse(sync.started_at)) / 1000)));
    update();
    const timer = setInterval(update, 1000);
    return () => clearInterval(timer);
  }, [loading, sync.started_at]);
  return h(Alert, { className: "items-sync", type: loading ? "info" : sync.status === "failed" ? "error" : "success", showIcon: true,
    icon: loading ? h(Spin, { size: "small" }) : undefined,
    message: `${sync.message} · ${loading ? `已用 ${elapsed} 秒` : `耗时 ${sync.elapsed_seconds} 秒`}`,
    description: loading ? "正在获取最新列表，完成后统一更新。" : sync.status === "failed" ? "本次未更新列表，请处理上述问题后手动刷新。" : undefined,
    style: { marginBottom: 16 } });
}
function Field(props) { return h("div", null, h("label", { className: "field-label" }, props.label), props.children); }
function mergeHistory(history, state) {
  if (!state) return history || [];
  const summary = Object.fromEntries(["id", "item", "items", "title", "stage", "updated_at", "last_dialogue_at", "last_user_message_at", "fix_round", "commits", "project", "can_move_project", "project_move_reason"].map(key => [key, state[key]]));
  summary.handoff_pending = Boolean(state.workspace_handoff_request);
  return [summary, ...(history || []).filter(entry => entry.id !== state.id)].sort(compareSessions);
}
function emptyDraft(state, settings) { return { supplement: state?.supplement || "", reply: "", plan_mode: false, review_enabled: false, answers: {}, review_note: "", selected_findings: [], review_number: state?.reviews?.at(-1)?.number || null,
  followup_compile_enabled: Boolean(state?.compile_check?.enabled ?? settings?.compile_check_enabled),
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

function ArtifactPreview({ sessionId, artifact, onClose, source = "artifact" }) {
  const [page, setPage] = useState(1);
  const [sheet, setSheet] = useState("");
  const [content, setContent] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const [imageError, setImageError] = useState(false);
  const base = source === "followup" ? "/api/followup-attachments" : "/api/artifacts";
  const address = { session_id: sessionId, [source === "followup" ? "attachment_id" : "artifact_id"]: artifact.id };
  const downloadUrl = `${base}${source === "followup" ? "" : "/download"}?${new URLSearchParams(address)}`;
  useEffect(() => {
    const controller = new AbortController();
    setLoading(true); setError(""); setContent(null); setImageError(false);
    const query = new URLSearchParams({ ...address, page, ...(sheet ? { sheet } : {}) });
    fetch(`${base}/preview?${query}`, { credentials: "same-origin", cache: "no-store", signal: controller.signal })
      .then(readResponse).then(setContent)
      .catch(error => { if (error.name !== "AbortError") setError(error.message); })
      .finally(() => { if (!controller.signal.aborted) setLoading(false); });
    return () => controller.abort();
  }, [sessionId, artifact.id, page, sheet, source]);
  const imageUrl = `${base}/image?${new URLSearchParams({ ...address, page })}`;
  const renderTable = (rows, key) => h("div", { className: "markdown-body", key }, h("table", null,
    h("tbody", null, rows.map((row, index) => h("tr", { key: index }, row.map((cell, column) =>
      h("td", { key: column }, cell === null ? "" : String(cell))))))));
  return h(Drawer, { title: `文件预览 · ${artifact.name}`, open: true, width: "min(100vw, 960px)", onClose,
    extra: h(Button, { href: downloadUrl, download: artifact.name }, "下载原文件") },
    loading && h(Spin, { tip: "正在读取预览…" }),
    error && h(Alert, { type: "error", showIcon: true, message: "预览失败", description: error }),
    content && h(React.Fragment, null,
      content.kind === "unsupported" && h(Alert, { type: "info", message: content.issue, showIcon: true }),
      content.issue && content.kind !== "unsupported" && h(Alert, { type: "warning", message: "部分内容未读取", description: content.issue, showIcon: true }),
      content.kind === "xlsx" && h(Space, { className: "artifact-toolbar", wrap: true },
        h(Select, { value: content.sheet, options: content.sheets.map(value => ({ value, label: value })),
          onChange: value => { setSheet(value); setPage(1); }, style: { minWidth: 180 } }),
        h(Typography.Text, { type: "secondary" }, "公式显示原文，下载后由 Excel 计算。")),
      ["text", "markdown"].includes(content.kind) && (content.kind === "markdown" ?
        h(Markdown, { text: content.items.join("\n") }) : h("pre", { className: "artifact-text" }, content.items.join("\n"))),
      content.kind === "docx" && content.items.map((block, index) => block.type === "table" ? renderTable(block.rows, index) :
        block.type === "image" ? h("div", { className: "attachment", key: index },
          h(Image, { src: `${base}/image?${new URLSearchParams({ ...address, image_id: block.image_id })}`,
            alt: `Word 图片 ${block.image_id}`, preview: { mask: "放大查看" } }), h("div", { className: "attachment-name" }, `图片 ${block.image_id}`)) :
        block.type === "list" ? h(block.ordered ? "ol" : "ul", { key: index }, block.items.map((text, item) => h("li", { key: item }, text))) :
        /^Heading [1-6]$/.test(block.style) ? h(`h${block.style.slice(-1)}`, { key: index }, block.text) :
          h("p", { key: index }, block.text)),
      content.kind === "xlsx" && renderTable(content.items, "cells"),
      ["image", "pdf"].includes(content.kind) && (imageError ? h(Alert, { type: "error", message: "图片预览失败，请下载原文件查看。" }) :
        h(Image, { src: imageUrl, alt: artifact.name, width: "100%", onError: () => setImageError(true), preview: { mask: "放大查看" } })),
      content.page && h(Space, { className: "artifact-toolbar", wrap: true },
        h(Button, { disabled: page <= 1 || loading, onClick: () => setPage(page - 1) }, "上一页"),
        h("span", null, content.kind === "pdf" ? `第 ${page} / ${content.total} 页` : `第 ${page} 页 · 共 ${content.total} ${content.kind === "docx" ? "段内容" : "行"}`),
        h(Button, { disabled: !content.has_next || loading, onClick: () => setPage(page + 1) }, "下一页"))));
}

function FollowupFiles({ sessionId, entries, editable, disabled, onRemove }) {
  const [preview, setPreview] = useState(null);
  return h(React.Fragment, null,
    h("div", { className: "attachment-grid" }, entries.map(entry => h("div", { className: "attachment", key: entry.id, "data-followup-file": entry.id },
      h("div", { className: "attachment-name" }, h("a", {
        href: `/api/followup-attachments?${new URLSearchParams({ session_id: sessionId, attachment_id: entry.id })}`, download: entry.name }, entry.name)),
      h(Typography.Text, { type: "secondary" }, `${(entry.size / 1024).toFixed(1)} KB · ${!entry.kind ? "只读取目录与 XML" :
        !entry.readable ? "内容未读取" : entry.kind === "pdf" ? `已准备 ${entry.page_count} 页供 AI 读取` :
        `${entry.kind === "docx" ? "正文和表格" : "文本"}可读取${entry.images.length ? ` · ${entry.images.length} 张图片` : ""}`}`),
      entry.issue && h(Typography.Paragraph, { type: "warning" }, entry.issue),
      !entry.kind && h(Collapse, { ghost: true, items: [{ key: "entries", label: "目录与读取状态", children:
        entry.entries.length ? entry.entries.map(member => h("div", { className: "attachment-name", key: member.member_id },
          h("div", null, member.name), h(Typography.Text, { type: member.issue ? "warning" : "secondary" },
            member.directory ? "目录" : `${member.extension.toUpperCase() || "无扩展名"} · ${member.size} 字节 · ${member.issue || (member.readable ? "XML 可读取" : "仅列出文件信息")}`))) :
          h(Typography.Text, { type: "secondary" }, "空 ZIP，无目录或文件") }] }),
      h(Space, { wrap: true }, entry.kind && h(Button, { size: "small", onClick: () => setPreview(entry) }, "预览"),
        editable && h(Tag, { color: "success" }, "已上传"), editable && h(Button, {
          size: "small", type: "text", danger: true, disabled, onClick: () => onRemove(entry) }, "移除"))))),
    preview && h(ArtifactPreview, { key: `${sessionId}:${preview.id}`, sessionId, artifact: preview, source: "followup", onClose: () => setPreview(null) }));
}

function ArtifactFiles({ sessionId, entries }) {
  const [preview, setPreview] = useState(null);
  if (!entries.length) return null;
  return h("div", { className: "artifact-files" }, h(Typography.Text, { strong: true }, "生成文件"),
    h("div", { className: "attachment-grid" }, entries.map(entry => {
      const query = new URLSearchParams({ session_id: sessionId, artifact_id: entry.id });
      return h("div", { className: "attachment", key: entry.id, "data-artifact-id": entry.id },
        h("div", { className: "attachment-name" }, entry.name),
        h(Typography.Text, { type: "secondary" }, `${entry.size.toLocaleString()} 字节`),
        entry.turn_status !== "completed" && h(Tag, null, `所属回合：${PROCESS_STATUS[entry.turn_status] || entry.turn_status}`),
        h(Space, { wrap: true }, h(Button, { size: "small", onClick: () => setPreview(entry) }, "预览"),
          h(Button, { size: "small", href: `/api/artifacts/download?${query}`, download: entry.name }, "下载")));
    })),
    preview && h(ArtifactPreview, { key: `${sessionId}:${preview.id}`, sessionId, artifact: preview, onClose: () => setPreview(null) }));
}

function WorkspaceHandoff({ state, repository, defaultTarget, busy, onAction, onClose }) {
  const request = state.workspace_handoff_request;
  const [target, setTarget] = useState(request?.target || defaultTarget);
  const [paths, setPaths] = useState(request?.selected_paths || []);
  useEffect(() => { setPaths(request?.selected_paths || []); }, [request?.token]);
  useEffect(() => { if (request && !busy && !state.error) setTarget(request.target); }, [request?.token, busy, state.error]);
  const started = Boolean(request?.started);
  const matches = request && request.target === target;
  const cancel = async () => {
    if (!request || await onAction("cancel_workspace_handoff", { token: request.token })) onClose();
  };
  return h(Modal, { title: `交接到本地开发目录 · ${repository.name}`, open: true, width: "min(800px, calc(100vw - 32px))",
    centered: true, styles: { body: { maxHeight: "60vh", overflow: "auto" } }, maskClosable: false,
    closable: !started, keyboard: !started, onCancel: cancel,
    footer: h(Space, { wrap: true }, h(Button, { disabled: busy, onClick: cancel }, started ? "已恢复原状，取消交接" : "取消"),
      !started && h(Button, { disabled: busy || !target.trim(), onClick: () => onAction("preview_workspace_handoff", { repository: repository.name, path: target.trim() }) }, request ? "重新预览" : "预览交接"),
      request && h(Button, { type: "primary", disabled: busy || !matches || request.already_here,
        onClick: () => onAction("confirm_workspace_handoff", { token: request.token, ...(started ? {} : { paths }) }) }, started ? "核对并继续交接" : "确认交接")) },
    h(Alert, { type: "info", showIcon: true, message: "保留会话，交接已提交的需求代码", description:
      "源目录会释放目标分支，原会话继续使用开发目录。勾选文件先备份；未勾选改动及 Git 忽略文件原地保留。同文件覆盖须先人工处理，交接不触发编译运行。" }),
    h(Form, { layout: "vertical", style: { marginTop: 16 } },
      h(Form.Item, { label: "本地开发目录" }, h(Input, { value: target, disabled: busy || started, "aria-label": "本地开发目录",
        onChange: event => setTarget(event.target.value) }))),
    (request?.error || state.error) && h(Alert, { type: "error", showIcon: true, message: "交接已停止", description: request?.error || state.error }),
    matches && h(React.Fragment, null,
      request.already_here && h(Alert, { type: "success", message: "已在该开发目录，无需交接。", showIcon: true }),
      h(Descriptions, { column: 1, size: "small", style: { marginTop: 16 }, items: [
        { key: "source", label: "源工作目录", children: h(Typography.Text, { copyable: true }, request.source) },
        { key: "target", label: "目标开发目录", children: h(Typography.Text, { copyable: true }, request.target) },
        { key: "old_branch", label: "开发目录原分支", children: request.target_branch || "分离 HEAD" },
        { key: "branch", label: "交接目标分支", children: request.branch },
        { key: "head", label: "交接提交", children: h(Typography.Text, { code: true, copyable: { text: request.source_head } }, request.source_head.slice(0, 12)) },
        ...(started ? [{ key: "progress", label: "交接进度", children: ({ backup: "备份本机文件", detach: "释放源分支", switch: "切换开发目录", verify: "核对文件与目录绑定" })[request.step] },
          { key: "backup", label: "备份位置", children: h(Typography.Text, { copyable: true }, request.backup) }] : []),
        ...(request.actual ? [{ key: "completed", label: "已完成步骤", children: request.completed_steps.length ? request.completed_steps.join("；") : "尚无已完成步骤" },
          ...["source", "target"].map(side => ({ key: side, label: side === "source" ? "停止时源目录状态" : "停止时开发目录状态",
            children: request.actual[side].head ? `${request.actual[side].branch === "HEAD" ? "分离 HEAD" : request.actual[side].branch} · ${request.actual[side].head.slice(0, 12)}` : "无法读取，请人工核对目录" }))] : [])] }),
      h(Typography.Paragraph, { style: { marginTop: 16 } }, "选择需要备份携带的本机改动（均不纳入需求提交）："),
      request.target_snapshot.paths.length ? h(Space, { direction: "vertical", style: { maxWidth: "100%" } }, request.target_snapshot.paths.map(path =>
        h(Checkbox, { key: path, checked: paths.includes(path), disabled: busy || started, style: { overflowWrap: "anywhere" },
          onChange: event => setPaths(event.target.checked ? [...paths, path] : paths.filter(value => value !== path)) }, path))) :
        h(Typography.Text, { type: "secondary" }, "目标目录没有本机改动。")));
}

function PromptText({ text }) {
  return h("div", { className: "prompt-text-block" },
    h("div", { className: "prompt-copy" }, h(Typography.Text, { copyable: { text, tooltips: ["复制全文", "已复制"] } }, "复制全文")),
    h("pre", { className: "description-text prompt-text" }, text));
}

function PromptStage({ stage }) {
  const [view, setView] = useState("templates");
  const [variantId, setVariantId] = useState(stage.templates[0]?.id);
  const variant = stage.templates.find(entry => entry.id === variantId);
  return h("div", { className: "card-stack" },
    h("div", null, h(Typography.Title, { level: 4, style: { marginTop: 0 } }, stage.title),
      h(Typography.Paragraph, null, stage.description),
      stage.controller && h(Tag, null, "控制器执行"),
      stage.id !== "common" && h(Typography.Text, { type: "secondary" }, `线程级规则：${stage.thread_rule}，原文见${stage.id === "branch" ? "本阶段模板" : "“通用规则”"}`)),
    stage.notes && h(Alert, { type: "info", showIcon: true, message: stage.notes }),
    h(Tabs, { activeKey: view, onChange: setView, items: [
      { key: "templates", label: "组装模板", children: h("div", { className: "card-stack" },
        stage.templates.length > 1 && h(Select, { "aria-label": "选择模板变体", value: variantId,
          options: stage.templates.map(entry => ({ value: entry.id, label: entry.title })), onChange: setVariantId, className: "prompt-variant" }),
        variant ? h("div", null,
          h(Space, { wrap: true, style: { marginBottom: 12 } }, h(Tag, { color: "blue" }, variant.layer), h(Typography.Text, { type: "secondary" }, variant.condition)),
          h(PromptText, { text: variant.content })) : h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: "由控制器执行，无独立模型提示词" })) },
      { key: "fragments", label: "组成片段", children: stage.fragments.length ? h(Collapse, { items: stage.fragments.map(entry => ({
        key: entry.id, label: entry.title, children: h("div", null,
          h(Typography.Paragraph, null, h(Typography.Text, { strong: true }, "生效条件："), entry.condition),
          h(Typography.Paragraph, { type: "secondary" }, "来源：", h(Typography.Text, { code: true }, entry.source)),
          h(PromptText, { text: entry.content })) })) }) : h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: "此阶段没有独立模型提示词片段" }) }
    ] }));
}

function PromptCatalogPage() {
  const [catalog, setCatalog] = useState(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [refresh, setRefresh] = useState(0);
  const [stageId, setStageId] = useState("plan");
  const screens = Grid.useBreakpoint();
  useEffect(() => {
    let active = true;
    setLoading(true); setError(""); setCatalog(null);
    fetch("/api/prompts", { credentials: "same-origin", cache: "no-store" }).then(readResponse)
      .then(value => { if (active) setCatalog(value); })
      .catch(reason => { if (active) setError(reason.message); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [refresh]);
  return h(CardBox, { title: h(Space, null, "工作流提示词", h(Tag, { color: "blue" }, "只读展示")),
    extra: h(Button, { onClick: () => setRefresh(value => value + 1), loading, disabled: loading }, "刷新") },
    h(Typography.Paragraph, { type: "secondary" }, "展示当前服务的固定原文与组装模板。占位符代表运行时数据；本页不读取具体会话资料。"),
    loading && h("div", { className: "prompt-loading" }, h(Spin, null), " 正在读取提示词…"),
    error && h(Alert, { type: "error", showIcon: true, message: "提示词读取失败", description: error,
      action: h(Button, { onClick: () => setRefresh(value => value + 1) }, "重新读取") }),
    catalog && h(React.Fragment, null,
      h(Tabs, { className: "prompt-stages", tabPosition: screens.lg ? "left" : "top", activeKey: stageId, onChange: setStageId,
        items: catalog.stages.map(stage => ({ key: stage.id, label: stage.title, children: h(PromptStage, { key: stage.id, stage }) })) }),
      h(Divider), h(Collapse, { items: [{ key: "placeholders", label: "占位符说明", children: h(List, { size: "small", dataSource: catalog.placeholders,
        renderItem: entry => h(List.Item, null, h("div", null, h(Typography.Text, { strong: true }, entry.name), h("div", { className: "muted" }, entry.description))) }) }] })));
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
  const [implementationPlanHidden, setImplementationPlanHidden] = useState(() => sessionStorage.getItem("workflow-implementation-plan-hidden") === "true");
  const eventSource = useRef(null);
  const [fatal, setFatal] = useState("");
  const [requestsPending, setRequestsPending] = useState({});
  const sending = requestsPending[model?.selectedSessionId || model?.preview?.preview_id || "new"] || requestsPending.new || requestsPending.global || "";
  const [sessionQuery, setSessionQuery] = useState("");
  const [sessionFilter, setSessionFilter] = useState("all");
  const [sessionNow, setSessionNow] = useState(Date.now);
  const [collapsedProjects, setCollapsedProjects] = useState(() => JSON.parse(localStorage.getItem("workflow-collapsed-projects") || "[]"));
  const [dragSessionId, setDragSessionId] = useState(null);
  const [dropProjectId, setDropProjectId] = useState(null);
  const viewEpoch = useRef(0);
  const deletedSessions = useRef(new Set());
  const [selectedItems, setSelectedItems] = useState([]);
  const selectionRequest = useRef("");
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [itemProject, setItemProject] = useState("all");
  const [itemPagination, setItemPagination] = useState({ current: 1, pageSize: 10 });
  const [draft, setDraft] = useState(emptyDraft());
  const draftRef = useRef(draft);
  const imageInput = useRef(null);
  const followupImageInput = useRef(null);
  const followupZipInput = useRef(null);
  const imageQueues = useRef({});
  const [imageTransfers, setImageTransfers] = useState({});
  const [materialsOpen, setMaterialsOpen] = useState(false);
  const [planDetailsOpen, setPlanDetailsOpen] = useState(false);
  const [diffOpen, setDiffOpen] = useState(false);
  const [baselineDirectoriesOpen, setBaselineDirectoriesOpen] = useState(false);
  const [handoffRepository, setHandoffRepository] = useState(null);
  const [diffs, setDiffs] = useState(null);
  const [settingsForm] = Form.useForm();
  const settingsModel = Form.useWatch("model", settingsForm);
  const settingsEffort = Form.useWatch("reasoning_effort", settingsForm);
  const [settingsSelection, setSettingsSelection] = useState(null);
  const [sessionSelection, setSessionSelection] = useState(null);
  const feedRef = useRef(null);
  const followBottom = useRef(true);
  const renderTimer = useRef(null);
  const lastSession = useRef(null);
  const lastSettings = useRef("");
  const [notice, noticeHolder] = notification.useNotification();
  const [modal, modalHolder] = Modal.useModal();

  function applyModel(value, render = true) {
    const selected = value.selectedSessionId ? value.sessions?.[value.selectedSessionId] : value.preview;
    const turn = selected?.state?.progress_turns?.at(-1);
    const reply = turn?.status === "running" && turn.items.findLast(item => ["agentMessage", "plan"].includes(item.type));
    const projected = { ...value, state: selected?.state || null, revision: selected?.revision,
      stream: reply && reply.phase !== "commentary" ? reply.text : selected?.stream || "", busy: selected?.busy || value.global_busy, working: selected?.working };
    modelRef.current = projected;
    if (render) {
      clearTimeout(renderTimer.current);
      renderTimer.current = null;
      setModel(projected);
    }
  }
  function queueModel(value) {
    applyModel(value, false);
    if (renderTimer.current === null) renderTimer.current = setTimeout(() => {
      renderTimer.current = null;
      setModel(modelRef.current);
    }, 100);
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
          if (task.purpose === "answer" && current.state.pending?.attachment_request_id !== task.answer_request_id) throw new Error("当前问题已经变化，请重新查看问题。");
          const query = new URLSearchParams({ ...viewAddress(id), revision: current.revision, name: task.name,
            ...(task.purpose === "answer" ? { answer_request_id: task.answer_request_id, question_id: task.question_id } : { purpose: task.purpose }) });
          const payload = await fetch(`/api/${task.purpose === "answer" ? "answer-attachments" : task.purpose === "followup_file" ? "followup-attachments" : "supplement-images"}?${query}`, { method: "POST", credentials: "same-origin",
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
  function addFollowupFiles(files) {
    const id = modelRef.current.state?.id;
    if (!id || !followupEditable) return;
    const queue = imageQueues.current[id] ||= { tasks: [], running: false };
    queue.tasks.push(...Array.from(files, file => ({ uid: crypto.randomUUID(), file, name: file.name,
      status: "waiting", error: "", purpose: file.type.startsWith("image/") || /\.(png|jpe?g|gif|webp|bmp)$/i.test(file.name) ? "followup" : "followup_file" })));
    publishImageTransfers();
    uploadImages(id);
  }
  function addAnswerFiles(files, questionId) {
    const current = modelRef.current.state;
    if (!current?.pending || !answerAttachmentEditable) return;
    const queue = imageQueues.current[current.id] ||= { tasks: [], running: false };
    queue.tasks.push(...Array.from(files, file => ({ uid: crypto.randomUUID(), file, name: file.name, status: "waiting", error: "",
      purpose: "answer", question_id: questionId, answer_request_id: current.pending.attachment_request_id })));
    publishImageTransfers();
    uploadImages(current.id);
  }
  function pasteAnswerImages(event, questionId, value, update) {
    const files = Array.from(event.clipboardData.items).filter(item => item.kind === "file" && item.type.startsWith("image/"))
      .map(item => item.getAsFile()).filter(Boolean);
    if (!files.length) return;
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain"), input = event.target;
    if (text) {
      const start = input.selectionStart, end = input.selectionEnd;
      update({ custom: (value.custom || "").slice(0, start) + text + (value.custom || "").slice(end) });
      requestAnimationFrame(() => input.setSelectionRange(start + text.length, start + text.length));
    }
    addAnswerFiles(files, questionId);
  }
  async function removeAnswerFile(entry) {
    const id = state.id;
    setRequestsPending(previous => ({ ...previous, [id]: "remove_attachment" }));
    try {
      const payload = await fetch("/api/answer-attachments/remove", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ session_id: id, revision: viewPayload(id).revision, answer_request_id: entry.request_id,
          question_id: entry.question_id, attachment_id: entry.id }) }).then(readResponse);
      if (modelRef.current.sessions[id]) receiveState(payload);
    } catch (error) { notice.error({ message: "移除附件失败", description: error.message }); }
    finally { setRequestsPending(previous => ({ ...previous, [id]: null })); }
  }
  async function removeFollowupFile(entry) {
    const id = state.id;
    setRequestsPending(previous => ({ ...previous, [id]: "remove_attachment" }));
    try {
      const payload = await fetch("/api/followup-attachments/remove", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ session_id: id, revision: viewPayload(id).revision, attachment_id: entry.id }) }).then(readResponse);
      if (modelRef.current.sessions[id]) receiveState(payload);
    } catch (error) { notice.error({ message: "移除附件失败", description: error.message }); }
    finally { setRequestsPending(previous => ({ ...previous, [id]: null })); }
  }
  function pasteImages(event, purpose = "supplement") {
    const files = Array.from(event.clipboardData.items).filter(item => item.kind === "file" && (purpose === "followup" || item.type.startsWith("image/")))
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
    if (purpose === "followup") addFollowupFiles(files);
    else addImages(files, purpose);
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
  async function newSession() {
    const projectId = modelRef.current.state?.project?.id || 0;
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
    setBaselineDirectoriesOpen(false);
    navigate("plan");
    await act("new_conversation", { project_id: projectId });
  }
  async function selectSession(id, targetPage) {
    const epoch = ++viewEpoch.current;
    discardPreview();
    selectionRequest.current = "";
    sessionStorage.setItem("workflow-session", id);
    applyModel({ ...modelRef.current, selectedSessionId: id });
    setDiffOpen(false);
    setBaselineDirectoriesOpen(false);
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
    localStorage.setItem("workflow-collapsed-projects", JSON.stringify(keys));
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
      for (const kind of ["state", "preview", "items", "items_sync", "connected", "connection", "conversation", "delta", "progress", "operation_error", "questions", "busy", "settings", "snapshot", "session_deleted", "projects"]) {
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
          else if (kind === "items_sync") applyModel({ ...current, items_sync: value });
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
            if (cached && cached.event_cursor < value.event_cursor) queueModel({ ...current, sessions: { ...current.sessions,
              [value.session_id]: { ...cached, stream: cached.stream + value.delta, event_cursor: value.event_cursor } } });
          }
          else if (kind === "progress") {
            const cached = current.sessions[value.session_id];
            if (cached && cached.event_cursor < value.event_cursor) {
              const payload = { ...cached, event_cursor: value.event_cursor,
                state: { ...cached.state, progress_turns: updateProgress(cached.state.progress_turns || [], value) } };
              const next = { ...current, sessions: { ...current.sessions, [value.session_id]: payload } };
              if (value.op === "delta") queueModel(next);
              else applyModel(next);
            }
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
    return () => { disposed = true; clearTimeout(renderTimer.current); if (events) events.close(); };
  }, []);
  useEffect(() => {
    window.addEventListener("pagehide", discardPreview);
    return () => window.removeEventListener("pagehide", discardPreview);
  }, []);
  useEffect(() => {
    const update = () => setSessionNow(Date.now());
    const timer = setInterval(update, 60000);
    document.addEventListener("visibilitychange", update);
    window.addEventListener("focus", update);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", update);
      window.removeEventListener("focus", update);
    };
  }, []);

  useEffect(() => { setMaterialsOpen(false); setPlanDetailsOpen(false); }, [model?.selectedSessionId, page]);
  useEffect(() => { setHandoffRepository(null); }, [model?.selectedSessionId, model?.state?.workspace_handoffs?.length]);

  const state = model?.state;
  const stage = state?.stage || "";
  const review = state?.reviews?.filter((entry, index) => (entry.delivery_round || 1) === (state.delivery_round || 1) && index >= (state.review_start || 0)).at(-1);
  const transfers = imageTransfers[state?.id] || [];
  const imagesUploading = transfers.some(entry => entry.status !== "error");
  const supplementEditable = Boolean(state && ["资料已读取", "准备工作区失败"].includes(stage) &&
    !stopped && !sending && !model?.busy && !state.cleanup?.removed?.length && !state.cleanup?.finalizing);
  const followupEditable = Boolean(state?.can_followup &&
    !stopped && !sending && !model?.busy && !model?.working && !state.cleanup?.removed?.length && !state.cleanup?.finalizing);
  const answerAttachmentEditable = Boolean(state?.pending && stage === "等待人工回答" && !stopped && !sending && !model?.busy &&
    !state.cleanup?.removed?.length && !state.cleanup?.finalizing &&
    (state.pending.method !== "workflow/ask_user" || state.pending.turn_completed === true));
  const answerRequestId = state?.pending?.attachment_request_id;
  useEffect(() => {
    const queue = imageQueues.current[state?.id];
    if (queue?.tasks.some(task => task.purpose === "answer" && task.answer_request_id !== answerRequestId)) {
      queue.tasks = queue.tasks.filter(task => task.purpose !== "answer" || task.answer_request_id === answerRequestId);
      publishImageTransfers();
    }
  }, [state?.id, answerRequestId]);
  const busy = Boolean(stopped || sending || model?.busy || (stage === "已暂停" && model?.working) || state?.cleanup?.removed?.length || state?.cleanup?.finalizing || imagesUploading);
  const canPause = ACTIVE.has(stage) || ["等待人工回答", "等待计划补充", "等待计划确认", "等待修复选择", "等待编译选择"].includes(stage);
  const selectedKey = selectedItems.join("|");
  const loadedKey = state ? sessionItems(state).map(itemKey).join("|") : "";
  const selectionMatches = Boolean(state?.snapshot) && selectedKey === loadedKey;
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
  useLayoutEffect(() => { followBottom.current = true; }, [page, state?.id]);
  useLayoutEffect(() => {
    if (feedRef.current && followBottom.current) feedRef.current.scrollTop = feedRef.current.scrollHeight;
  }, [page, state?.id, selectionMatches, state?.phase, model?.stream, state?.history?.length, state?.progress_turns, review?.number]);
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
    const id = ["connect", "refresh", "settings", "new_conversation"].includes(action) ? null : targetId;
    const previewId = !id && ["collect", "plan"].includes(action) ? current.preview?.preview_id : null;
    const pendingKey = id || previewId || (["collect", "new_conversation"].includes(action) ? "new" : "global");
    const epoch = viewEpoch.current;
    setRequestsPending(previous => ({ ...previous, [pendingKey]: action }));
    try {
      const result = await fetch("/api/actions", { method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Workflow-Token": csrf.current },
        body: JSON.stringify({ action, data, request_id: crypto.randomUUID(), session_id: id,
          preview_id: previewId, revision: previewId ? current.preview.revision : current.sessions[id]?.revision }) }).then(readResponse);
      if (["collect", "new_conversation"].includes(action) && !id && epoch === viewEpoch.current) {
        if (!previewId) changeDraft({ codex: { model: current.settings.model, reasoning_effort: current.settings.reasoning_effort } });
        lastSession.current = result.preview_id;
        applyModel({ ...modelRef.current, preview: { preview_id: result.preview_id, event_cursor: -1 } });
        const payload = await fetch(`/api/preview?id=${encodeURIComponent(result.preview_id)}`, { cache: "no-store" }).then(readResponse);
        if (epoch === viewEpoch.current) receivePreview(payload);
      } else if (["collect", "new_conversation"].includes(action) && !id && !previewId) {
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
    if (state.can_followup) {
      const images = state.followup_images.filter(image => image.history_index === undefined);
      const attachments = state.followup_attachments.filter(entry => entry.history_index === undefined);
      if (busy || transfers.length) return;
      if (!draft.reply.trim() && !images.length && !attachments.length) { notice.warning({ message: "请填写消息、添加图片或上传文件" }); return; }
      const id = state.id;
      const result = await act("followup", { text: draft.reply, image_ids: images.map(image => image.id), attachment_ids: attachments.map(entry => entry.id), plan_mode: draft.plan_mode, review_enabled: draft.review_enabled,
        followup_compile_enabled: draft.followup_compile_enabled });
      if (result) {
        if (modelRef.current.selectedSessionId === id) changeDraft({ reply: "", answers: {}, plan_mode: false, review_enabled: false });
        else {
          const saved = JSON.parse(sessionStorage.getItem(`workflow-draft:${id}`) || "{}");
          sessionStorage.setItem(`workflow-draft:${id}`, JSON.stringify({ ...saved, reply: "", answers: {}, plan_mode: false, review_enabled: false }));
        }
      }
      return;
    }
    const questions = state?.pending?.params?.questions || [];
    if (transfers.some(task => task.purpose === "answer" && task.answer_request_id === answerRequestId)) return;
    const answers = {}, attachmentIds = {};
    for (const question of questions) {
      const key = `${state.pending.id}:${question.id}:${question.question}`;
      const value = draft.answers[key] || {};
      const text = [value.choice, value.custom?.trim()].filter(Boolean).join("\n");
      const files = (state.answer_attachments || []).filter(entry => entry.request_id === answerRequestId && entry.question_id === question.id && entry.history_index === undefined);
      if (!text && !files.length) { notice.warning({ message: "尚未回答完整", description: "请逐项选择、填写答案或上传附件。" }); return; }
      answers[question.id] = text ? [text] : [];
      if (files.length) attachmentIds[question.id] = files.map(entry => entry.id);
    }
    const text = draft.reply.trim();
    if (!text && !questions.length) { notice.warning({ message: "请先填写回复" }); return; }
    const id = state.id;
    if (await act("answer", { text, answers: questions.length ? answers : null,
      ...(questions.length ? { attachment_ids: attachmentIds, answer_request_id: answerRequestId } : {}) }) && modelRef.current.selectedSessionId === id) changeDraft({ reply: "", answers: {} });
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

  function renderWorkItemMaterials(snapshot, address) {
    if (!snapshot) return h(Empty, { description: "本会话暂无已保存的工作项资料" });
    return h(React.Fragment, null,
      !snapshot.sources.length && h(Empty, { description: "本会话暂无已保存的工作项资料" }),
      snapshot.blockers?.length > 0 && h(Alert, { type: "warning", showIcon: true, message: "有待补充的需求资料", description: h("div", null, snapshot.blockers.map((text, index) => h("div", { key: index }, text))), style: { marginBottom: 20 } }),
      snapshot.sources.map((source, index) => h("div", { className: "source-block", key: `${source.kind}:${source.id}:${index}` },
        h("div", { className: "source-heading" }, h(Tag, { color: source.selected ? "blue" : "default" }, source.selected || (source.selected == null && index === 0) ? "选中工作项" : "关联资料"), h("strong", null, source.title), h(Typography.Text, { type: "secondary" }, `${TYPE_NAMES[source.kind]} #${source.id}`),
          source.version != null && h(Tag, null, `关联版本 ${source.version}`)),
        source.note && h(Typography.Text, { type: "secondary" }, source.note),
        h("pre", { className: "description-text" }, plainDescription(source.content) || (source.origin === "task" ? "任务详情未提供关联需求正文，可结合任务描述分析" : "该工作项没有有效描述，请在计划前补充目标和验收标准。")),
        h("div", { className: "attachment-grid" }, snapshot.materials.filter(material => source.material_ids.includes(material.file_id)).map(material => {
          const url = `/api/material?${new URLSearchParams({ ...address, file_id: material.file_id })}`;
          const fileName = material.title.toLowerCase().endsWith(`.${material.extension}`) ? material.title : `${material.title}.${material.extension}`;
          return h("div", { className: "attachment", key: material.file_id }, material.image && h(Image, { src: url, alt: material.title, preview: { mask: "查看图片" } }),
            h("div", { className: "attachment-name" }, material.image ? `${material.title} · ${material.file_id}` : h("a", { href: url }, fileName)),
            material.issue && h(Typography.Text, { type: "warning" }, material.issue),
            renderPdfPages(material, url),
            material.archive_entries?.map((entry, index) => {
              const entryUrl = `${url}&entry=${index}`;
              return h("div", { className: "attachment", key: index },
                entry.image && h(Image, { src: entryUrl, alt: entry.title, preview: { mask: "查看图片" } }),
                h("div", { className: "attachment-name" }, entry.path ? h("a", { href: entryUrl }, entry.title) : entry.title),
                entry.issue && h(Typography.Text, { type: "warning" }, entry.issue), renderPdfPages(entry, entryUrl));
            }));
      })))),
      snapshot.feishu_links?.length > 0 && h(Alert, { type: "info", showIcon: true, message: "飞书文档暂未接入", style: { marginTop: 18 },
        description: h("div", null, h("p", null, "如果文档中有必要业务规则，请在计划前补充相关内容。"), snapshot.feishu_links.map(url => h("div", { key: url }, h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, url)))) }));
  }

  function renderItems() {
    const projects = [...new Map(model.items.map(item => [item.project_id, {
      value: item.project_id, label: itemProjectLabel(item)
    }])).values()];
    const rows = model.items.filter(item => (filter === "all" || item.kind === filter) &&
      (itemProject === "all" || item.project_id === itemProject) &&
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
      { title: "类型", dataIndex: "kind", width: 110, render: typeTag },
      { title: "编号", dataIndex: "id", width: 90, render: value => h(Typography.Text, { code: true }, `#${value}`) },
      { title: "标题", dataIndex: "title", render: value => h("span", { className: "table-title" }, value) },
      { title: "状态", dataIndex: "status", width: 100, render: value => h(Tag, null, STATUS_NAMES[value] || value) },
      { title: "所属项目", dataIndex: "project", width: 120, render: (_, item) => itemProjectLabel(item) },
      { title: "优先级", dataIndex: "priority", width: 80, render: value => value ? `P${value}` : "—" }
    ];
    const snapshot = selectionMatches ? state?.snapshot : null;
    return h("div", { className: "card-stack" },
      h(CardBox, { title: "待处理工作项", extra: h(Space, null, h(Typography.Text, { type: "secondary" }, `共 ${model.items.length} 项`), h(Button, { onClick: () => act("refresh"), disabled: busy || !model.connected, loading: model.busy === "refresh" }, "刷新列表")) },
        model.items_sync && h(ItemsSyncStatus, { sync: model.items_sync }),
        h("div", { className: "table-tools" }, h(Radio.Group, { value: filter, onChange: event => { setFilter(event.target.value); setItemPagination(previous => ({ ...previous, current: 1 })); }, optionType: "button", options: [{ label: "全部", value: "all" }, ...Object.entries(TYPE_NAMES).map(([value, label]) => ({ value, label: `${label} ${model.items.filter(item => item.kind === value).length}` }))] }),
          h(Space, { wrap: true },
            h(Select, { "aria-label": "筛选所属项目", value: itemProject, showSearch: true, optionFilterProp: "label", style: { width: 180 },
              options: [{ value: "all", label: "全部项目" }, ...projects],
              onChange: value => { setItemProject(value); setItemPagination(previous => ({ ...previous, current: 1 })); } }),
            h(Input.Search, { placeholder: "搜索编号或标题", allowClear: true, value: query,
              onChange: event => { setQuery(event.target.value); setItemPagination(previous => ({ ...previous, current: 1 })); }, style: { width: 260 } }))),
        h(Table, { columns, dataSource: rows, rowKey: itemKey, size: "middle", pagination: {
          ...itemPagination, current: Math.min(itemPagination.current, Math.max(1, Math.ceil(rows.length / itemPagination.pageSize))),
          showSizeChanger: true, pageSizeOptions: [10, 20, 50], showTotal: total => `共 ${total} 项`,
          onChange: (current, pageSize) => setItemPagination({ current: pageSize === itemPagination.pageSize ? current : 1, pageSize }) },
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
        snapshot && state.item === null && h(Typography.Paragraph, { type: "secondary" }, "尚未关联工作项，可直接输入需求并开始计划，也可勾选工作项读取资料。"),
        stage === "资料读取失败" && h(Alert, { type: "error", showIcon: true, message: "资料读取失败", description: state.error,
          action: h(Button, { disabled: busy || !model.connected, onClick: () => act("collect", { items: selectedItems.map(key => {
            const [kind, id] = key.split(":"); return { kind, id: Number(id) };
          }) }) }, "重新读取资料"), style: { marginBottom: 16 } }),
        !snapshot ? h("div", { className: "empty-area" }, h(Empty, { description: model.busy === "collect" || sending === "collect" ? "正在读取所选工作项的完整资料…" : "勾选工作项后自动读取描述、图片、附件及关联需求" })) :
          renderWorkItemMaterials(snapshot, model.preview ? { preview_id: state.id } : { session: state.id })),
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
      h(CardBox, { size: "small", className: "app-card sidebar-card project-info-card", title: "项目信息",
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
          editable && !model.preview && h(Button, { block: true, disabled: transfers.length > 0, onClick: () => act("edit_branches", { supplement: draft.supplement }) }, "更换项目常用分支"),
          waiting && h(React.Fragment, null,
            h("p", { className: "project-info-note" }, "人工选择将保存为项目常用分支，后续优先复用。"),
            h(Button, { type: "primary", block: true, disabled: busy || unresolved.some(entry => !entry.catalog.some(branch => branch.name === choices[entry.role])),
              onClick: () => act("choose_branches", { choices: Object.fromEntries(unresolved.map(entry => [entry.role, choices[entry.role]])) }) }, "保存为项目常用分支并继续"),
            h(Button, { type: "text", block: true, disabled: busy || Boolean(state.repos.length), onClick: startPlan }, "重新识别分支")))));
  }

  function renderImplementationPlan() {
    const feedbackAuthorized = state.approval_source === "followup";
    const approved = state.approved_hash === state.plan_hash;
    const planStatus = feedbackAuthorized ? "原计划 · 业务背景" : approved && state.phase === "complete" ? "计划已完成" : stage;
    return h("section", { id: "implementation-plan", "aria-label": implementationPlanTitle },
      h(CardBox, { size: "small", className: "app-card sidebar-card implementation-plan-card", title: implementationPlanTitle,
        extra: h(Space, { size: 0 },
          h(Button, { type: "link", size: "small", disabled: !state.plan, onClick: () => setPlanDetailsOpen(true) }, "查看详情"),
          h(Button, { type: "text", size: "small", "aria-label": `隐藏${implementationPlanTitle}`, onClick: () => {
            setImplementationPlanHidden(true); sessionStorage.setItem("workflow-implementation-plan-hidden", "true");
          } }, "隐藏")) },
        state.plan ? h(React.Fragment, null,
          h("div", { className: "plan-status" }, stageTag(planStatus),
            approved && !feedbackAuthorized && h(Tag, null, state.approval_source === "auto" ? "自动授权实施" : "人工确认实施")),
          h("div", { className: "plan-panel" }, h(Markdown, { text: state.plan })),
          h("div", { className: "plan-version" }, `计划版本 ${state.plan_hash.slice(0, 12)}`)) :
          h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: "完成需求澄清后生成计划" }),
        h(Button, { type: "primary", block: true, disabled: stage !== "等待计划确认" || busy,
          onClick: () => act("approve_plan", { plan_hash: state.plan_hash }) }, "确认当前计划并实施")));
  }

  function renderBranches({ compact = false } = {}) {
    const transfers = (imageTransfers[state.id] || []).filter(task => task.purpose === "supplement");
    const editable = ["资料已读取", "准备工作区失败"].includes(stage) && !busy && !state.workspace_reuse_request;
    const repositories = state.branch_resolution?.repositories || [];
    const checkout = state.checkout_request;
    const reuse = state.workspace_reuse_request;
    const canReuse = !state.prepare_complete && ["plan", "followup"].includes(state.phase) &&
      ["准备工作区失败", "已停止", "已暂停"].includes(stage) && !state.pending && !state.manual_compile &&
      !Object.keys(state.commits || {}).length && (!checkout || ["waiting", "restored"].includes(checkout.status));
    const reuseCandidates = (state.selections || []).filter(selection => !state.repos.some(repo => repo.name === selection.name));
    const checkoutKey = checkout && [checkout.path, checkout.head, checkout.baseline, checkout.snapshot.fingerprint].join("\n");
    const checkoutPaths = draft.checkout_key === checkoutKey ? draft.checkout_paths || [] : [];
    const detailRepositories = state.branch_resolution?.manual && !state.repos.length ? [] : model.settings.repositories.filter(repository => {
      const entry = repositories.find(entry => entry.role === repository.role);
      return state.repos.some(entry => entry.name === repository.name) || entry?.status === "needs_user" ||
        entry?.reason || entry?.question || entry?.candidates.length > 0 || entry?.refresh_state || entry?.fetch_state;
    });
    return h("div", { className: "card-stack" },
      canReuse && reuseCandidates.length > 0 && h(CardBox, { title: "复用已有工作区" },
        h("p", { className: "muted" }, "原目标分支已在其他工具创建的工作区检出时，可先预览，核对后明确确认复用。其他未完成会话的目录不能接管。"),
        h("div", { className: "action-bar" }, reuseCandidates.map(selection => h(Button, {
          key: selection.name, disabled: busy || model.working,
          onClick: () => act("preview_workspace_reuse", { repository: selection.name }) }, `预览复用已有工作区（${selection.name}）`))),
        reuse && h(React.Fragment, null,
          h(Alert, { type: "warning", showIcon: true, message: "以当前 HEAD 建立本轮初始基线", description:
            "该提交及之前的代码不纳入本轮差异。已有未提交修改按本机保留内容排除，不自动修改或提交；确认后继续原会话，删除会话保留此目录。" }),
          h(Descriptions, { column: 1, size: "small", items: [
            { key: "name", label: "仓库", children: reuse.name },
            { key: "path", label: "工作目录", children: h(Typography.Text, { copyable: true }, reuse.path) },
            { key: "branch", label: "原目标分支", children: reuse.branch },
            { key: "head", label: "本轮初始基线", children: h(Typography.Text, { code: true, copyable: true }, reuse.head) },
            { key: "subject", label: "最近提交", children: reuse.subject },
            { key: "occupancy", label: "会话占用", children: reuse.occupancy },
            { key: "preserved", label: "本机保留文件", children: reuse.snapshot.paths.length ? reuse.snapshot.paths.join("、") : "无" }] }),
          h("div", { className: "action-bar" },
            h(Button, { disabled: busy || model.working, onClick: () => act("cancel_workspace_reuse") }, "取消复用"),
            h(Button, { disabled: busy || model.working,
              onClick: () => act("preview_workspace_reuse", { repository: reuse.name }) }, "重新预览"),
            h(Button, { type: "primary", disabled: busy || model.working,
              onClick: () => act("confirm_workspace_reuse", { token: reuse.token }) }, "确认复用并继续")))),
      stage === "准备工作区失败" && checkout && h(CardBox, { title: "保留本机改动并切换分支" },
        h("p", null, `${checkout.selection.name}：${checkout.from_branch} → ${checkout.selection.branch}`),
        h(Typography.Paragraph, { copyable: true }, checkout.path),
        checkout.status === "waiting" ? h(React.Fragment, null,
          h(Alert, { type: "info", showIcon: true, message: "仅将勾选文件存入 stash 备份，切换后合并恢复。未勾选改动继续留在目录中；恢复冲突时停止并保留备份。" }),
          h("p", { className: "muted" }, "保留文件不自动修改，不参与本轮需求审查和提交。请逐项勾选，不会自动选择。"),
          h(Space, { direction: "vertical" }, checkout.snapshot.paths.map(path => h(Checkbox, {
            key: path, checked: checkoutPaths.includes(path), disabled: busy,
            onChange: event => changeDraft({ checkout_key: checkoutKey,
              checkout_paths: event.target.checked ? [...checkoutPaths, path] : checkoutPaths.filter(value => value !== path) }) }, path))),
          h("div", { className: "action-bar" },
            h(Button, { disabled: busy, onClick: startPlan }, "重新检查本机改动"),
            h(Button, { type: "primary", disabled: busy || !checkoutPaths.length,
              onClick: () => act("smart_checkout", { paths: checkoutPaths }) }, "携带选中改动并继续"))) :
          checkout.baseline_change && !checkout.baseline_change.confirmed ? h(React.Fragment, null,
            h(Alert, { type: "info", showIcon: true, message: "目标分支已快进更新。请核对提交变化，确认后以新提交建立本轮工作区基准并继续。" }),
            h(Descriptions, { column: 1, size: "small", items: [
              { key: "previous", label: "旧基准", children: h(Typography.Text, { code: true, copyable: true }, checkout.baseline_change.previous) },
              { key: "current", label: "新基准", children: h(Typography.Text, { code: true, copyable: true }, checkout.baseline_change.current) }] }),
            h("p", { className: "muted" }, "仅准备阶段允许确认；本机保留文件仍不参与本轮需求修改、审查和提交，stash 备份继续保留。"),
            h("div", { className: "action-bar" }, h(Button, { type: "primary", disabled: busy,
              onClick: () => act("checkout_resolved", { baseline: checkout.baseline_change.current }) }, "确认使用新基准并继续"))) :
          checkout.status !== "restored" && h(React.Fragment, null,
            h(Alert, { type: "warning", showIcon: true, message: "智能切换已停止。请手工完成目标分支切换、恢复备份改动并解决冲突，再确认继续。" }),
            h("div", { className: "action-bar" }, h(Button, { type: "primary", disabled: busy,
              onClick: () => act("checkout_resolved") }, "已手工恢复改动并解决冲突，继续"))),
        checkout.selected_paths && h("p", { className: "muted" }, `本次保留文件：${checkout.selected_paths.join("、")}`),
        checkout.stash && h(Typography.Paragraph, { copyable: true }, `本机改动备份（保留）：${checkout.stash}`)),
      ACTIVE.has(stage) && state.phase === "branch" &&
      h(CardBox, { title: "项目与分支操作" },
        h(Space, null, h(Spin, { size: "small" }), stage)),
      detailRepositories.length > 0 && h("div", { className: compact ? "card-stack" : "repository-grid" }, detailRepositories.map(repository => {
        const entry = repositories.find(entry => entry.role === repository.role);
        const workspace = state.repos.find(entry => entry.name === repository.name);
        const folded = compact && Boolean(workspace);
        return h(folded ? DetailSection : CardBox, { className: folded ? "repository-details" : "app-card repository-card",
          ...(folded ? { summary: workspace.branch } : {}), key: repository.name,
          title: `${repository.name}分支与工作区` },
          (entry?.reason || workspace) && h("p", { className: "muted" }, entry?.reason || "沿用当前会话已创建工作区的分支。"),
          entry?.question && h(Alert, { type: "warning", showIcon: true, message: entry.question }),
          entry?.candidates.length > 0 && h("p", { className: "muted" }, `AI 候选：${entry.candidates.join("、")}`),
          entry?.refresh_state && h("p", { className: "muted" }, { running: "正在核对远程分支清单…", done: "已核对远程分支清单", failed: "远程分支清单核对失败，修复连接后可恢复当前阶段" }[entry.refresh_state]),
          entry?.fetch_state && h("p", { className: "muted" }, `${{ running: "正在拉取目标分支", done: "已拉取目标分支", failed: "目标分支拉取失败" }[entry.fetch_state]}：${entry.fetch_branch}`),
          workspace && h(React.Fragment, null,
            h(Descriptions, { column: 1, size: "small", style: { marginTop: 18 }, items: [{ key: "commit", label: "起始提交", children: h(Typography.Text, { code: true }, workspace.baseline.slice(0, 12)) }, { key: "path", label: workspace.workspace_owned === false ? "复用工作目录" : "独立工作区", children: h(Typography.Text, { style: { fontSize: 12 }, copyable: true }, workspace.path) }, ...(workspace.preserved_paths?.length ? [{ key: "preserved", label: "本机保留改动", children: `${workspace.preserved_paths.length} 个文件，不自动修改或提交` }] : []), ...(workspace.checkout_backup ? [{ key: "checkout_backup", label: "本机改动备份", children: h(Typography.Text, { copyable: true }, workspace.checkout_backup) }] : [])] }),
            h(Tooltip, { title: state.handoff_reason || "将已完成需求交接到原 IDEA 开发目录，保留本机改动" }, h("span", null,
              h(Button, { size: "small", disabled: busy || model.working || Boolean(state.handoff_reason) || Boolean(state.workspace_handoff_request),
                onClick: () => setHandoffRepository(workspace.name) }, "交接到本地开发目录")))));
      })),
      (["资料已读取", "准备工作区失败"].includes(stage) || state.supplement_images?.length > 0 || transfers.length > 0) &&
        h(CardBox, { title: state.item === null ? "输入需求与验收标准" : "补充需求与验收标准" },
          h("div", { onPaste: supplementEditable ? pasteImages : undefined },
            h(Input.TextArea, { value: draft.supplement, rows: 6, disabled: !supplementEditable,
              placeholder: state.item === null ? "直接输入需求、问题或修改要求，可粘贴截图或图片，无需关联工作项…" : "补充业务规则、验收标准，可在此粘贴截图或图片…", onChange: event => changeDraft({ supplement: event.target.value }) }),
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
            h(Button, { type: "primary", size: "large", disabled: !editable || (checkout && !["waiting", "restored"].includes(checkout.status)) || transfers.length > 0 || (!state.codex && !sessionSelection) ||
              (state.item === null && !draft.supplement.trim() && !state.supplement_images.length), onClick: startPlan }, stage === "准备工作区失败" ? "重新准备工作区并继续" : state.item === null ? "发送并开始计划" : "开始需求分析与计划"))));
  }

  function renderFeedbackImages(images, editable = false, purpose = "followup") {
    return h("div", { className: "attachment-grid" }, images.map(image => h("div", { className: "attachment", key: image.id },
      h(Image, { src: `/api/supplement-images?${new URLSearchParams({ session_id: state.id, purpose, image_id: image.id })}`,
        alt: image.name, preview: { mask: "查看图片" } }),
      h("div", { className: "attachment-name" }, image.name),
      editable && h("div", { className: "supplement-image-actions" }, h(Button, { size: "small", type: "text", danger: true,
        onClick: () => removeImage(image, "followup") }, "移除")))));
  }

  function renderPdfPages(material, url) {
    return material.pdf_pages?.map((page, index) => h("div", { className: "attachment", key: index },
      h(Image, { src: `${url}&page=${index}`, alt: page.title, preview: { mask: "查看 PDF 页面" } }),
      h("div", { className: "attachment-name" }, `第 ${page.page} 页 · 已准备供 Codex 读取`)));
  }

  function renderFollowupFiles(entries, editable = false) {
    return h(FollowupFiles, { key: state.id, sessionId: state.id, entries, editable, disabled: !followupEditable, onRemove: removeFollowupFile });
  }

  function renderAnswerFiles(entries, editable = false, showQuestion = false) {
    return h("div", { className: "attachment-grid" }, entries.map(entry => {
      const url = `/api/answer-attachments?${new URLSearchParams({ session_id: state.id, attachment_id: entry.id })}`;
      return h("div", { className: "attachment", key: entry.id },
        entry.image && h(Image, { src: url, alt: entry.name, preview: { mask: "查看图片" } }),
        h("div", { className: "attachment-name" }, h("a", { href: url, download: entry.name }, entry.name)),
        showQuestion && h(Typography.Text, { type: "secondary" }, entry.question),
        editable && h("div", { className: "supplement-image-actions" }, h(Tag, { color: "success" }, "已上传"),
          h(Button, { size: "small", type: "text", danger: true, disabled: !answerAttachmentEditable,
            onClick: () => removeAnswerFile(entry) }, "移除")));
    }));
  }
  function renderFileTransfers(tasks, editable) {
    return h("div", { className: "attachment-grid" }, tasks.map(task =>
      h("div", { className: "attachment", key: task.uid }, h("div", { className: "attachment-name" }, task.name),
        h("div", { className: "supplement-image-actions" }, task.status === "error" ? h(Tag, { color: "error" }, "上传失败") :
          h(Space, null, h(Spin, { size: "small" }), task.status === "waiting" ? "等待上传" : "上传中")),
        task.status === "error" && h(React.Fragment, null, h(Typography.Text, { type: "danger" }, task.error),
          h(Space, null, h(Button, { size: "small", disabled: !editable, onClick: () => {
            task.status = "waiting"; task.error = ""; publishImageTransfers(); uploadImages(state.id);
          } }, "重试"), h(Button, { size: "small", type: "text", danger: true, disabled: !editable, onClick: () => {
            const queue = imageQueues.current[state.id]; queue.tasks = queue.tasks.filter(entry => entry.uid !== task.uid); publishImageTransfers();
          } }, "移除"))))));
  }

  function renderFeedbackTransfers() {
    return renderFileTransfers(transfers.filter(task => ["followup", "followup_file"].includes(task.purpose)), followupEditable);
  }

  function renderPlan() {
    if (!state || !selectionMatches) return h(CardBox, null, h(Empty, { description: "点击新建会话输入需求，或选择工作项并等待读取完成" }));
    if (!model.selectedSessionId) return renderBranches();
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
    const continuing = Boolean(state.can_followup);
    const answerFiles = (state.answer_attachments || []).filter(entry => entry.request_id === answerRequestId && entry.history_index === undefined);
    const answerTransfers = transfers.filter(task => task.purpose === "answer" && task.answer_request_id === answerRequestId);
    const answersComplete = questions.every(question => {
      const value = draft.answers[`${state.pending.id}:${question.id}:${question.question}`] || {};
      return value.choice || value.custom?.trim() || answerFiles.some(entry => entry.question_id === question.id);
    });
    const canAnswer = (["等待人工回答", "等待计划补充", "等待计划确认"].includes(stage) || continuing) && !busy &&
      (state.pending?.method !== "workflow/ask_user" || state.pending.turn_completed === true);
    const reviewsByMessage = new Map(state.reviews.map(entry => [entry.history_index, entry]));
    const entries = [...state.history.map((entry, index) => ({ ...entry, index })),
      ...(state.progress_turns || []).map(turn => ({ process: turn, index: turn.history_index }))]
      .filter(entry => entry.role !== "控制器" || entry.display_text).sort((left, right) => left.index - right.index || Boolean(right.process) - Boolean(left.process));
    const processRunning = state.progress_turns?.at(-1)?.status === "running";
    return h("div", { className: "card-stack conversation-layout", key: state.id },
      h(Modal, { title: "变更仓库目录与审查基线", open: baselineDirectoriesOpen,
        onCancel: () => setBaselineDirectoriesOpen(false), footer: h(Space, null,
          h(Button, { disabled: busy, onClick: () => setBaselineDirectoriesOpen(false) }, "取消"),
          h(Button, { type: "primary", disabled: busy, onClick: async () => {
            const paths = Object.fromEntries(state.repos.map(repo => [repo.name,
              draft.baseline_paths?.[repo.name] ?? state.baseline_change_request?.repositories.find(entry => entry.name === repo.name)?.path ?? repo.path]));
            if (await act("preview_baseline", { paths })) setBaselineDirectoriesOpen(false);
          } }, "预览目录与基线")) },
        h(Alert, { type: "info", showIcon: true, message: "更换仅对当前会话生效", description:
          "填写已检出原目标分支的 Git 仓库根目录。保留原线程和历史消息；旧目录文件留在原处，新目录已有改动按本机保留文件排除。下一步预览后仍须明确确认。" }),
        h(Form, { layout: "vertical", style: { marginTop: 16 } }, state.repos.map(repo =>
          h(Form.Item, { key: repo.name, label: `${repo.name}仓库目录（分支：${repo.branch}）` },
            h(Input, { "aria-label": `${repo.name}仓库目录`, disabled: busy,
              value: draft.baseline_paths?.[repo.name] ?? state.baseline_change_request?.repositories.find(entry => entry.name === repo.name)?.path ?? repo.path,
              onChange: event => changeDraft({ baseline_paths: { ...draft.baseline_paths, [repo.name]: event.target.value } }) }))))),
        h(CardBox, { title: "工作流会话", extra: stageTag(stage) },
          state.codex && h(Typography.Paragraph, { type: "secondary" }, `模型：${state.codex.model} · 推理强度：${EFFORT_NAMES[state.codex.reasoning_effort] || state.codex.reasoning_effort}（${state.codex.reasoning_effort}）`),
          state.phase === "complete" && h(Alert, { showIcon: true, type: stage === "已完成（未审查）" ? "info" : "success",
            message: `第 ${state.delivery_round} 轮已完成`, "aria-label": "本轮执行结果", style: { marginBottom: 16 },
            description: stage === "已完成（未审查）" ? `${state.review_enabled ? "本轮无代码修改，已跳过 Review" : "本轮自动 Review 已关闭"}；未执行本地提交。` :
              "Review 已完成，本地提交已完成，可在下方查看审查结论及本地提交记录。" }),
          h("div", { className: "conversation-feed", ref: feedRef, onScroll: event => {
            const feed = event.currentTarget;
            followBottom.current = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 48;
          } }, entries.length === 0 && h(Empty, { description: "等待开始计划" }),
            entries.map(entry => entry.process ? h(ProgressTurn, { key: entry.process.id, turn: entry.process }) :
              h("div", { className: `chat-entry ${entry.role === "用户" ? "user" : ["系统", "控制器"].includes(entry.role) ? "system" : "assistant"}`, key: entry.index },
              h("div", { className: "chat-meta" }, h("strong", null, entry.role === "控制器" ? "系统" : entry.role), h("span", null, timeLabel(entry.time)),
                entry.delivery_round > 1 && h(Tag, null, `第 ${entry.delivery_round} 轮交付`),
                typeof entry.plan_mode === "boolean" && h(Tag, null, `计划模式：${entry.plan_mode ? "开启" : "关闭"}`),
                typeof entry.review_enabled === "boolean" && h(Tag, null, `修改后自动 Review：${entry.review_enabled ? "开启" : "关闭"}`),
                typeof entry.compile_check_enabled === "boolean" && h(Tag, null, `后端编译检查：${entry.compile_check_enabled ? "开启" : "关闭"}`)),
              h("div", { className: `chat-body${reviewsByMessage.has(entry.index) || entry.review_output ? " review-message" : ""}` },
                reviewsByMessage.has(entry.index) ? renderReview(reviewsByMessage.get(entry.index), entry.text) :
                  entry.phase === "review" && entry.role === "Codex" ?
                    stage === "审查中" ? h("span", { className: "muted" }, "正在校验审查结果…") :
                      entry.review_output ? renderReview({ result: entry.review_output }, entry.text, entry.review_error) : h("pre", { className: "review-original" }, entry.text) :
                    entry.role === "Codex" ? h(Markdown, { text: entry.text }) : entry.display_text || entry.text,
                entry.images?.length > 0 && renderFeedbackImages(entry.images, false, entry.image_purpose || "followup"),
                entry.followup_attachments?.length > 0 && renderFollowupFiles(entry.followup_attachments),
                entry.attachments?.length > 0 && renderAnswerFiles(entry.attachments, false, true),
                h(ArtifactFiles, { key: `${state.id}:${entry.index}`, sessionId: state.id,
                  entries: (state.artifacts || []).filter(file => file.history_index === entry.index) })))),
            h(ArtifactFiles, { key: `${state.id}:unattached`, sessionId: state.id,
              entries: (state.artifacts || []).filter(file => file.history_index === undefined) }),
            !model.stream && !processRunning && ["准备工作区中", "计划中", "实施中", "修复中", "续聊处理中", "编译修复中", "编译分析中"].includes(stage) &&
              h("div", { className: "stream-indicator", role: "status" }, h(Spin, { size: "small" }),
                stage === "准备工作区中" ? " 正在准备工作区…" : " 模型正在处理中…"),
            state.phase === "review" && stage === "审查中" ? h("div", { className: "stream-indicator", role: "status" }, h(Spin, { size: "small" }), " 正在审查，结果校验完成后显示问题卡片…") :
              model.stream && h("div", { className: "chat-entry assistant" }, h("div", { className: "stream-indicator" }, "正在分析…"), h("div", { className: "chat-body" }, h(Markdown, { text: model.stream })))),
          h(ChangedFiles, { key: state.id, sessionId: state.id, revision: model.revision,
            running: !stopped && (model.working || Boolean(model.busy) || ACTIVE.has(stage)) }),
          state.phase === "complete" && h(ChangeSummary, { key: `${state.id}:locations`, pages: state.change_summary })),
        h(CardBox, { className: "app-card followup-composer", title: questions.length ? "需要你的回答" : continuing ? "后续对话" : "补充说明与回复",
          extra: hasWorkItemMaterials && h(Button, { size: "small", "aria-controls": "work-item-materials", "aria-expanded": materialsOpen,
            onClick: () => setMaterialsOpen(true) }, "工作项资料") },
          questions.map(question => {
            const key = `${state.pending.id}:${question.id}:${question.question}`;
            const value = draft.answers[key] || {};
            function update(change) { changeDraft({ answers: { ...draft.answers, [key]: { ...value, ...change } } }); }
            return h("div", { className: "question", key }, h("div", { className: "question-title" }, question.question),
              question.options?.length > 0 && h(Radio.Group, { value: value.choice, onChange: event => update({ choice: event.target.value }), disabled: !canAnswer, className: "question-options" },
                question.options.map(option => h("div", { key: option.label }, h(Radio, { value: option.label }, option.label), option.description && h("div", { className: "option-note" }, option.description)))),
              h(Input.TextArea, { value: value.custom || "", rows: 2, disabled: !canAnswer, placeholder: "也可以填写自定义答案或补充说明",
                onPaste: answerAttachmentEditable ? event => pasteAnswerImages(event, question.id, value, update) : undefined,
                onChange: event => update({ custom: event.target.value }) }),
              h("div", { className: "supplement-image-tools" },
                h("input", { type: "file", hidden: true, multiple: true, "aria-label": `为问题“${question.question}”上传文件`, onChange: event => {
                  addAnswerFiles(event.target.files, question.id); event.target.value = "";
                } }),
                h(Button, { disabled: !answerAttachmentEditable, onClick: event => event.currentTarget.parentElement.querySelector("input").click() }, "上传文件"),
                h(Typography.Text, { type: "secondary" }, `支持任意格式、多选及粘贴图片，单个不超过 ${model.settings.max_attachment_mb} MB`)),
              renderAnswerFiles(answerFiles.filter(entry => entry.question_id === question.id), true),
              renderFileTransfers(answerTransfers.filter(task => task.question_id === question.id), answerAttachmentEditable),
              answerTransfers.some(task => task.question_id === question.id && task.status === "error") &&
                h(Typography.Text, { type: "warning" }, "请重试或移除失败附件后再提交。"));
          }),
          continuing && h("div", { className: "supplement-image-tools followup-upload-tools" },
            h("input", { type: "file", ref: followupImageInput, hidden: true, multiple: true, accept: ".png,.jpg,.jpeg,.gif,.webp,.bmp", "aria-label": "上传续聊图片", onChange: event => {
              addImages(event.target.files, "followup"); event.target.value = "";
            } }),
            h(Button, { disabled: !followupEditable, onClick: () => followupImageInput.current.click() }, "上传图片"),
            h("input", { type: "file", ref: followupZipInput, hidden: true, multiple: true,
              accept: ".pdf,.docx,.txt,.md,.csv,.tsv,.json,.xml,.html,.log,.sql,.zip,.png,.jpg,.jpeg,.gif,.webp,.bmp", "aria-label": "上传续聊文件", onChange: event => {
              addFollowupFiles(event.target.files); event.target.value = "";
            } }),
            h(Button, { disabled: !followupEditable, onClick: () => followupZipInput.current.click() }, "上传文件"),
            h(Tooltip, { title: "支持 PDF、Word、文本、图片及 ZIP；ZIP 仅读取目录与 XML" },
              h(Typography.Text, { type: "secondary", className: "muted" }, `可多选或粘贴，单个 ≤ ${model.settings.max_attachment_mb} MB`))),
          h(Input.TextArea, { rows: 3, value: draft.reply, disabled: !canAnswer,
            onPaste: continuing && followupEditable ? event => pasteImages(event, "followup") : undefined,
            placeholder: questions.length ? "其他补充说明（可选）" : continuing ? "输入问题、需要获取的信息或修改要求，可粘贴图片和文件…" : "输入回复、需要调整的计划内容或补充业务规则…", onChange: event => changeDraft({ reply: event.target.value }) }),
          continuing && h(React.Fragment, null,
            renderFeedbackImages(state.followup_images.filter(image => image.history_index === undefined), followupEditable),
            renderFollowupFiles(state.followup_attachments.filter(entry => entry.history_index === undefined), followupEditable),
            renderFeedbackTransfers(),
            transfers.some(task => task.status === "error") && h(Typography.Text, { type: "warning" }, "请重试或移除失败附件后再发送。")),
          h("div", { className: continuing ? "action-bar followup-actions" : "action-bar" },
            continuing && h("div", { className: "followup-options" },
              h(Tooltip, { title: "先生成计划" }, h("label", { className: "followup-option" },
                h(Switch, { size: "small", checked: draft.plan_mode, disabled: !followupEditable, "aria-label": "计划模式", onChange: checked => changeDraft({ plan_mode: checked }) }),
                h("span", null, "计划模式"))),
              h(Tooltip, { title: "有改动时审查，通过后本地提交" }, h("label", { className: "followup-option" },
                h(Switch, { size: "small", checked: draft.review_enabled, disabled: !followupEditable, "aria-label": "修改后自动 Review", onChange: checked => changeDraft({ review_enabled: checked }) }),
                h("span", null, "自动审查"))),
              h(Tooltip, { title: "检查后端并修复相关问题" }, h("label", { className: "followup-option" },
                h(Switch, { size: "small", checked: draft.followup_compile_enabled, disabled: !followupEditable, "aria-label": "本轮后端编译检查", onChange: checked => changeDraft({ followup_compile_enabled: checked }) }),
                h("span", null, "后端检查")))),
            h(Button, { type: "primary", disabled: !canAnswer || !answersComplete || answerTransfers.length > 0 || (continuing && transfers.length > 0), onClick: answer }, questions.length ? "提交回答并继续" : continuing ? "发送" : "提交补充说明"))),
        h(CardBox, { title: "流程操作" }, h(Space, { wrap: true },
          h(Button, { disabled: stopped || !canPause, onClick: () => act("pause") }, "暂停当前阶段"),
          h(Button, { disabled: busy || !["已暂停", "已停止"].includes(stage), onClick: () => act("resume") }, "从暂停点继续"),
          h(Button, { disabled: busy || model.working || !state.prepare_complete || Boolean(state.pending) || Boolean(state.manual_compile) ||
            !state.repos.some(repo => repo.role === "backend") || ["plan", "branch", "compile_fix", "compile_analysis"].includes(state.phase) ||
            !["已暂停", "已停止", "等待修复选择", "等待编译选择", "已完成（静态审查）", "已完成（未审查）"].includes(stage),
            onClick: () => act("compile_now") }, "立即编译检查"),
          h(Button, { disabled: busy || model.working || !state.prepare_complete || Boolean(state.pending) || Boolean(state.manual_compile) || Object.keys(state.commits || {}).length > 0 ||
            !["已暂停", "已停止", "等待计划确认", "等待计划补充", "等待修复选择", "等待编译选择"].includes(stage),
            onClick: () => setBaselineDirectoriesOpen(true) }, "变更目录与审查基线"),
          h(Button, { disabled: !review, onClick: () => document.getElementById(`review-${review.number}`)?.scrollIntoView({ behavior: "smooth", block: "start" }) }, "定位最新 Review")),
          h("p", { className: "muted", style: { margin: "15px 0 0", lineHeight: 1.8 } }, continuing || state.followup ?
            "后续消息可手动开启计划模式；实施或审查中请先暂停再补充，原目标与补充需求合并处理。计划模式和自动 Review 开关均在发送后恢复关闭。需要澄清时等待明确回答；审查通过后仅本地 commit，不 push。" :
            "未提问且一次成稿的计划自动实施；有提问或多回合补充时等待确认。Review 无遗留问题后自动本地 commit，不 push。")),
        state.baseline_change_request && h(CardBox, { title: "确认变更审查基线" },
          h(Alert, { type: "warning", showIcon: true, message: "将当前 HEAD 设为本轮新基线", description:
            "原线程、聊天、计划、附件和历史记录保留，确认前备份完整会话。旧基线下的审查及检查结论失效；新基线之前的已提交代码将移出本轮审查范围。更换目录时旧文件留在原处，新目录已有改动全部排除；不自动复制代码。" }),
          state.baseline_change_request.repositories.map(repo => h("div", { key: repo.name, style: { marginTop: 16 } },
            h(Typography.Text, { strong: true }, `${repo.name} · ${repo.branch} · ${repo.relationship}`),
            h(Descriptions, { column: 1, size: "small", items: [
              { key: "old", label: "旧基线", children: h(Typography.Text, { code: true, copyable: true }, repo.previous) },
              { key: "new", label: "新基线", children: h(Typography.Text, { code: true, copyable: true }, repo.current) },
              { key: "path", label: "工作目录", children: h(Typography.Text, { copyable: true }, repo.path) },
              ...(repo.directory_changed ? [
                { key: "old_path", label: "原工作目录", children: h(Typography.Text, { copyable: true }, repo.previous_path) },
                { key: "old_files", label: "旧目录改动（不搬迁）", children: repo.previous_remaining_paths.length ? repo.previous_remaining_paths.join("、") : "无未提交任务文件" }] : []),
              { key: "remaining", label: "保留的本轮差异", children: repo.remaining_paths.length ? repo.remaining_paths.join("、") : "无未提交任务文件" },
              { key: "preserved", label: "本机保留文件", children: repo.preserved_paths.length ? repo.preserved_paths.join("、") : "无" }] }),
            repo.stat && h(Collapse, { ghost: true, items: [{ key: "stat", label: "将移出或改变本轮审查范围的提交差异", children: h("pre", { className: "review-original" }, repo.stat) }] }))),
          h("div", { className: "action-bar" },
            h(Button, { disabled: busy, onClick: () => act("cancel_baseline") }, "取消变更"),
            h(Button, { disabled: busy, onClick: () => setBaselineDirectoriesOpen(true) }, "重新预览"),
            h(Button, { type: "primary", disabled: busy, onClick: () => act("confirm_baseline", { token: state.baseline_change_request.token }) }, "确认变更并保留会话"))),
        renderCompileChecks(),
        renderBranches({ compact: true }),
        Object.keys(state.commits || {}).length > 0 && h(DetailSection, { title: "本地提交记录", summary: `${Object.keys(state.commits).length} 个仓库` }, h(Descriptions, { column: 1, items: Object.entries(state.commits).map(([name, sha]) => ({ key: name, label: name, children: h(Typography.Text, { copyable: true, code: true }, sha) })) })),
        state.deliveries.length > 0 && h(DetailSection, { title: "历史交付记录", summary: `${state.deliveries.length} 轮交付` }, h(Collapse, { ghost: true, items: [...state.deliveries].reverse().map(delivery => ({
          key: delivery.number, label: `第 ${delivery.number} 轮交付 · ${timeLabel(delivery.completed_at)}${typeof delivery.review_enabled === "boolean" ? ` · 自动 Review ${delivery.review_enabled ? "开启" : "关闭"}` : ""}${delivery.stage ? ` · ${delivery.stage}` : ""}`,
          children: h(Descriptions, { column: 1, items: Object.entries(delivery.commits).map(([name, sha]) => ({ key: name, label: name,
            children: h(Typography.Text, { code: true, copyable: true }, sha) })) })
        })) })));
  }

  function renderCompileChecks() {
    const checks = state.compile_checks || [];
    const latest = checks.at(-1);
    const statusNames = { pending: "等待检查", running: "正在检查", passed: "通过", skipped: "人工跳过",
      dependencies: "缺少依赖", blocked: "检查未通过", code_error: "代码错误", excluded: "不再检查",
      waiting: "等待处理", repairing: "正在修复", analyzing: "正在分析", complete: "检查完成", invalidated: "结论已失效" };
    const items = checks.map((batch, index) => ({
          key: batch.id, label: h(Space, { size: "small", wrap: true },
            `第 ${index + 1} 次检查 · 第 ${batch.delivery_round} 轮交付 · 自动修复 ${batch.fix_round}/${batch.limit}`,
            batch.manual_request_id && h(Tag, null, "独立检查"),
            h(Tag, null, statusNames[batch.status])),
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
                ["blocked", "dependencies", "code_error"].includes(entry.status) &&
                  (entry.failure_reasons?.length > 0 || !entry.diagnostics?.length) && h(Alert, {
                  type: "warning", showIcon: true, message: "失败原因", description: entry.failure_reasons?.length > 0 ?
                    h("ul", { className: "markdown-body" }, entry.failure_reasons.map((reason, number) => h("li", { key: number }, reason))) :
                    entry.failure_reasons ? "本次未提取到具体诊断，可先分析失败原因。" : "该历史记录未保存具体失败原因，请重新检查以取得诊断。" }),
                entry.modules.length > 0 && h(Typography.Paragraph, { type: "secondary" }, `模块：${entry.modules.join("、")}（含所需模块）`),
                entry.diagnostics?.map((diagnostic, number) => h("p", { key: number }, `${diagnostic.path}:${diagnostic.line} · ${diagnostic.message}`)),
                entry.analysis && h("div", null, h("strong", null, "失败分析"), h(Markdown, { text: entry.analysis })),
                retired && h(Button, { disabled: busy, onClick: () => choose("retry") }, "前端检查已取消，继续后端流程"),
                current && h(Space, { wrap: true },
                  entry.status === "dependencies" && h(Button, { type: "primary", disabled: busy, onClick: () => choose("download") }, "补齐依赖并重新检查（允许下载）"),
                  entry.status === "code_error" && batch.fix_round < batch.limit && h(Button, { type: "primary", disabled: busy, onClick: () => choose("fix") }, "修复代码并重新检查"),
                  ["blocked", "dependencies", "code_error"].includes(entry.status) && h(Button, { disabled: busy, onClick: () => choose("analyze") }, "分析失败原因"),
                  h(Button, { disabled: busy, onClick: () => choose("retry") }, "重新检查"),
                  h(Button, { disabled: busy, onClick: () => choose("skip") }, "跳过该仓库本轮检查"),
                  entry.status === "code_error" && batch.fix_round >= batch.limit && h(Button, { disabled: busy, onClick: () => choose("add_round") }, "追加一轮编译修复"),
                  h(Button, { disabled: busy, onClick: () => choose("pause") }, "暂停")));
            })),
        }));
    const active = latest?.delivery_round === state.delivery_round && ["running", "waiting", "repairing", "analyzing"].includes(latest.status);
    const result = latest?.status === "complete" ? [...new Set(latest.repositories.filter(entry => entry.status !== "excluded")
      .map(entry => statusNames[entry.status]))].join("、") || "无需检查" : latest ? statusNames[latest.status] : "尚无记录";
    return h(DetailSection, { title: "后端编译检查", className: "compile-checks",
      summary: h(Space, { size: "small", wrap: true }, h(Tag, null, state.compile_check?.enabled ? "已开启" : "已关闭"),
        h(Tag, { color: active ? latest.status === "waiting" ? "gold" : "processing" : latest?.status === "complete" ? "success" : "default" }, result),
        checks.length > 0 && h("span", null, `${checks.length} 次检查`)),
      defaultOpen: active, expansionKey: latest ? `${latest.id}:${latest.status}:${latest.fix_round}` : "empty" },
      latest ? h(React.Fragment, null,
        h("div", { className: "compile-current" }, h("div", { className: "compile-batch-summary" }, items.at(-1).label), items.at(-1).children),
        checks.length > 1 && h(Collapse, { className: "compile-history", size: "small", ghost: true,
          items: [{ key: "history", label: `历史检查记录（${checks.length - 1} 次）`,
            children: h(Collapse, { size: "small", ghost: true, items: items.slice(0, -1).reverse() }) }] })) :
        h(Typography.Text, { type: "secondary" }, state.compile_check?.enabled ? "实施或修复完成后，按模块检查有任务改动的后端；前端继续参与 Review。" : "本会话不自动执行后端编译检查。"));
  }

  function renderReview(entry, originalText, validationError = "") {
    const latest = !validationError && entry.number === review?.number && (entry.delivery_round || 1) === state.delivery_round && state.phase !== "complete";
    const findings = entry.result.findings;
    const skipped = new Set(entry.skipped_ids || []);
    const pending = findings.filter(finding => !skipped.has(finding.id));
    const selected = latest ? draft.selected_findings.filter(id => pending.some(finding => finding.id === id)) : [];
    const limitReached = state.fix_round >= state.max_fix_rounds;
    const canSelect = latest && !busy && ["等待修复选择", "部分提交"].includes(stage);
    const canFix = latest && stage === "等待修复选择" && !busy;
    const canCommit = canSelect && !selected.length;
    return h("section", { className: "review-content", id: validationError ? undefined : `review-${entry.number}`, "aria-label": validationError ? "未通过校验的 Review" : `第 ${entry.number} 次 Review` },
      h("div", { className: "review-heading" }, h("strong", null, validationError ? "未通过校验的 Review" : `第 ${entry.number} 次 Review`),
        validationError ? h(Tag, { color: "error" }, "校验失败 · 只读") : h(React.Fragment, null,
          h(Tag, null, latest ? `修复 ${state.fix_round} / ${state.max_fix_rounds} 轮` : `修复第 ${entry.fix_round} 轮后`), !latest && h(Tag, null, "历史记录 · 只读"))),
      validationError && h(Alert, { type: "error", showIcon: true, message: "此结果不能作为修复或提交依据", description: validationError }),
      h("div", { className: "review-summary" }, h("div", { className: "review-number" }, pending.length, h("span", null, validationError ? "返回问题" : "待处置问题")), h("div", { className: "review-copy" }, h(Markdown, { text: entry.result.summary }))),
      h("div", { className: "finding-list" }, findings.length === 0 && h(Empty, { image: Empty.PRESENTED_IMAGE_SIMPLE, description: validationError ? "返回结果中没有问题，尚未通过校验" : "本轮没有发现待处理问题" }),
        findings.map((finding, index) => h("div", { className: `finding-card${selected.includes(finding.id) ? " selected" : ""}`, key: validationError ? index : finding.id },
          h("div", { className: "finding-title" }, h(Checkbox, { checked: selected.includes(finding.id), disabled: !canSelect || skipped.has(finding.id),
            onChange: event => changeDraft({ selected_findings: event.target.checked ? [...selected, finding.id] : selected.filter(id => id !== finding.id) }) },
            h(Tag, { color: ["P0", "P1"].includes(finding.severity) ? "error" : finding.severity === "P2" ? "warning" : "default" }, finding.severity),
            h("strong", null, finding.title))),
          h("div", { className: "finding-meta" }, h(Typography.Text, { code: true }, validationError ? `问题 ${index + 1}` : finding.id), h(Tag, null, finding.category),
            h(Tag, { color: finding.origin === "本次引入" ? "volcano" : finding.origin === "证据不足" ? "gold" : "default" }, finding.origin), skipped.has(finding.id) && h(Tag, null, "不修复")),
          h("div", { className: "finding-location" }, `${finding.repository}/${finding.path}:${finding.line}`),
          h(Collapse, { ghost: true, size: "small", items: [{ key: "details", label: "判断依据、处理建议与业务影响",
            children: h("div", { className: "finding-details" }, ...[["判断依据", finding.evidence], ["处理建议", finding.suggestion], ["业务影响", finding.impact]].map(([label, value]) => h("div", { key: label }, h("strong", null, label), h(Markdown, { text: value })))) }] })))),
      latest && h("div", { className: "review-actions" },
        h(Button, { onClick: openDiff, disabled: stopped }, "查看累计代码差异"),
        limitReached && pending.length > 0 && h(Alert, { type: "warning", showIcon: true, style: { marginTop: 14 }, message: "已达到本次修复轮次上限", description: "如需继续修复，请明确追加一轮；也可选择不修复剩余问题并本地提交。" }),
        h(Field, { label: "修复说明（可选）" }, h(Input.TextArea, { rows: 3, value: draft.review_note, disabled: !canSelect,
          placeholder: "可补充选中问题的修复要求，无需填写不修复原因。", onChange: event => changeDraft({ review_note: event.target.value }) })),
        h("div", { className: "action-bar" },
          h(Button, { disabled: !canFix, onClick: () => act("add_round") }, "追加一轮"),
          h(Button, { disabled: busy || !["等待修复选择", "已暂停", "已停止"].includes(stage), onClick: () => act("rereview") }, "重新 Review"),
          h(Button, { type: "primary", disabled: !canFix || !selected.length || limitReached, onClick: () => act("fix", { ids: selected, note: draft.review_note }) }, `修复选中 ${selected.length} 项并复审`)),
        h(Divider), h("div", { className: "review-finalize" },
          h("span", { className: "muted" }, pending.length ? "只勾选要修复的问题；点击修复后，未选项本轮不再修复，无需填写原因" : state.phase === "complete" ? "本地提交已完成，未 push" : "无待处理问题时自动本地提交；提交失败后可手动继续"),
          h(Button, { disabled: !canCommit, onClick: () => act("finalize", { ids: pending.map(finding => finding.id), note: "" }) }, pending.length ? "不修复剩余问题并本地提交" : "继续本地提交"))),
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
        .sort(compareSessions);
      return { ...project, rows, lastDialogueTime: entries.reduce((latest, entry) => Math.max(latest, dialogueTime(entry)), -Infinity) };
    }).filter(project => !project.id || project.manual || (!query && sessionFilter === "all") || project.rows.length)
      .sort((left, right) => !left.id ? 1 : !right.id ? -1 : left.lastDialogueTime === right.lastDialogueTime ? left.id - right.id : right.lastDialogueTime - left.lastDialogueTime);
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
                const inactive = Boolean(entry.last_user_message_at && sessionNow - Date.parse(entry.last_user_message_at) > 120 * 60 * 60 * 1000);
                const canMove = entry.can_move_project && !stopped && !requestsPending[entry.id];
                const cannotDelete = stopped || entry.working || entry.busy || entry.handoff_pending || ACTIVE.has(entry.stage) || entry.stage === "等待人工回答" || entry.stage === "资料读取中";
                const open = () => selectSession(entry.id, ["资料已读取", "资料读取失败"].includes(entry.stage) ? "items" : "plan");
                return h(List.Item, { key: entry.id, className: `session-entry${selected ? " selected" : ""}${inactive ? " inactive" : ""}`, onClick: open, draggable: Boolean(canMove),
                  title: entry.project_move_reason || "可拖动到其他项目分区",
                  onDragStart: event => { event.dataTransfer.setData("application/x-workflow-session", entry.id); event.dataTransfer.effectAllowed = "move"; setDragSessionId(entry.id); },
                  onDragEnd: () => { setDragSessionId(null); setDropProjectId(null); },
                  tabIndex: 0, role: "button", "aria-pressed": selected, onKeyDown: event => { if (event.target === event.currentTarget && ["Enter", " "].includes(event.key)) { event.preventDefault(); open(); } } },
                  h("div", { className: "session-entry-body" },
                    h("div", { className: "session-entry-custom-title", title: entry.title }, entry.title),
                    h("div", { className: "session-entry-title", title: itemLabel(entry) }, sessionItems(entry).map(item => h("div", { key: itemKey(item) }, `${TYPE_NAMES[item.kind]} #${item.id} · ${item.title}`))),
                    h("div", { className: "session-entry-status" }, stageTag(entry.stage), entry.busy && h(Spin, { size: "small" }),
                      inactive && h(Tag, { className: "session-inactive-tag", title: `用户最后发言：${timeLabel(entry.last_user_message_at)}` }, "超过5天未对话")),
                    h("div", { className: "session-entry-foot" }, h("span", { className: "muted", title: "最近对话时间" }, entry.last_dialogue_at ? timeLabel(entry.last_dialogue_at) : "尚无对话"),
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
  const renderers = { items: renderItems, plan: renderPlan, prompts: () => h(PromptCatalogPage), settings: renderSettings };
  const menuIcons = ["▦", "☷", "▤", "⚙"];
  const workPage = page === "items" || page === "plan";
  const hasWorkItemMaterials = workPage && model.selectedSessionId && state?.id === model.selectedSessionId && sessionItems(state).length > 0;
  const hasProjectInfo = workPage && selectionMatches;
  const showProjectInfo = hasProjectInfo && !projectInfoHidden;
  const implementationPlanTitle = state?.approval_source === "followup" ? "原需求实施计划" : "实施计划";
  const hasImplementationPlan = page === "plan" && model.selectedSessionId && selectionMatches &&
    (state.codex || ["资料已读取", "资料读取失败"].includes(stage)) && state.phase !== "branch" && (state.repos.length > 0 || state.followup);
  const showImplementationPlan = hasImplementationPlan && !implementationPlanHidden;
  const showSidebar = showProjectInfo || showImplementationPlan;
  const handoffName = state?.workspace_handoff_request?.repository || handoffRepository;
  const handoffWorkspace = state?.repos.find(repo => repo.name === handoffName);
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
          workPage && state?.error && stage !== "资料读取失败" && h(Alert, { type: "error", showIcon: true, message: "当前流程需要处理", description: state.error, style: { marginBottom: 16 } }),
          h("div", { className: "page-heading" }, h("div", null, h("h1", null, heading[0]), h("div", { className: "page-subtitle" }, heading[1]))),
          h("div", { className: workPage ? "session-workspace" : "" }, workPage && renderSessions(), h("div", { className: "session-content" },
          workPage && h(Card, { className: "workflow-strip", size: "small" }, h("div", { className: "workflow-strip-top" }, h("div", { className: "current-item", title: state ? itemLabel(state) : "" }, state ? itemLabel(state) : "选择工作项，开始一次完整的需求交付"),
            h("div", { className: "workflow-strip-actions" }, stageTag(stage), hasProjectInfo && projectInfoHidden &&
              h(Button, { size: "small", "aria-controls": "project-info", "aria-expanded": false, onClick: () => {
                setProjectInfoHidden(false); sessionStorage.setItem("workflow-project-info-hidden", "false");
              } }, "项目信息"), hasImplementationPlan && implementationPlanHidden &&
              h(Button, { size: "small", "aria-controls": "implementation-plan", "aria-expanded": false, onClick: () => {
                setImplementationPlanHidden(false); sessionStorage.setItem("workflow-implementation-plan-hidden", "false");
              } }, implementationPlanTitle))),
            h(Steps, { current: stepOf(state), status: stage === "已完成（未审查）" ? "finish" : undefined, size: "small", responsive: false,
              items: [state?.item === null ? "输入需求" : "选择工作项", "制定计划", state?.followup ? "处理消息" : "实施修改", "Review", "本地提交"].map((title, index) =>
                stage === "已完成（未审查）" && index > 2 ? { title: `${title}（跳过）`, status: "wait" } : { title }) })),
          h("div", { className: `session-body${showSidebar ? " with-sidebar" : ""}` },
            h("div", { className: "workspace-main" }, renderers[page]()), showSidebar &&
              h("div", { className: "session-sidebar card-stack" }, showProjectInfo && renderProjectInfo(), showImplementationPlan && renderImplementationPlan())))), h("div", { className: "page-footer" }, "数据保存在本机 · 关闭页面不会取消已确认的后台任务")))) ,
    h(Drawer, { title: "工作项资料", "aria-label": "工作项资料", placement: "right", width: "min(960px, 100vw)", rootClassName: "materials-drawer",
      open: Boolean(materialsOpen && hasWorkItemMaterials), onClose: () => setMaterialsOpen(false), destroyOnClose: true },
      materialsOpen && hasWorkItemMaterials && h("div", { id: "work-item-materials" },
        h(Typography.Paragraph, { type: "secondary" }, "本会话资料快照"),
        h(Space, { wrap: true, style: { marginBottom: 16 } }, ...sessionItems(state).map(item =>
          h(Tag, { key: itemKey(item), color: "blue" }, `${TYPE_NAMES[item.kind]} #${item.id} · ${item.title}`))),
        renderWorkItemMaterials(state.snapshot, { session: state.id }))),
    handoffWorkspace && h(WorkspaceHandoff, { key: `${state.id}:${handoffName}`, state, repository: handoffWorkspace,
      defaultTarget: state.handoff_targets[handoffName] ?? "", busy: busy || model.working, onAction: act, onClose: () => setHandoffRepository(null) }),
    h(Modal, { title: `${implementationPlanTitle}详情`, className: "implementation-plan-details", centered: true,
      width: "min(1200px, calc(100vw - 32px))", open: Boolean(planDetailsOpen && hasImplementationPlan && state.plan),
      onCancel: () => setPlanDetailsOpen(false), footer: h(Button, { onClick: () => setPlanDetailsOpen(false) }, "关闭"), destroyOnClose: true },
      planDetailsOpen && hasImplementationPlan && h(React.Fragment, null,
        h("div", { className: "plan-version" }, `计划版本 ${state.plan_hash.slice(0, 12)}`),
        h(Markdown, { text: state.plan }))),
    h(Drawer, { title: "相对初始基线的累计代码差异", placement: "right", width: "80vw", open: diffOpen, onClose: () => setDiffOpen(false) },
      diffs === null ? h(Spin, null) : h(Collapse, { defaultActiveKey: diffs.map((_, index) => String(index)), items: diffs.map((diff, index) => ({ key: String(index), label: diff.repository,
        children: diff.commit ? h(Typography.Text, { copyable: true }, diff.commit) : h(React.Fragment, null, h("pre", { className: "diff-text" }, diff.stat || "没有已跟踪文件差异"), diff.patch && h("pre", { className: "diff-text" }, diff.patch),
          diff.new_files.map(file => h("div", { key: file.path }, h("strong", null, file.path), h("pre", { className: "diff-text" }, file.patch || file.note)))) })) })));
}

ReactDOM.createRoot(document.getElementById("root")).render(h(WorkflowApp));
