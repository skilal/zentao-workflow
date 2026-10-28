# 禅道需求开发工作流 · 网页版

在浏览器中选择指派给当前账号的需求、任务或 Bug，人工选择仓库与分支，完成需求澄清和计划确认后实施，再按固定提示词 Review、选择修复项并循环复审，最后本地 commit。

网页只展示和控制流程；禅道访问、Codex 会话及 Git 操作均由本机 Python 服务完成。服务固定监听 `127.0.0.1`，不发布到外部站点。

## 启动与停止

使用 Windows、Python 3.11 或以上、Git，以及已登录的 Codex CLI。运行时只使用 Python 标准库和已随项目保存的前端资源，不需要 Tkinter、Node、npm 或联网加载页面组件。

首次下载并配置：

```powershell
git clone https://github.com/skilal/zentao-workflow.git zentao-codex-workflow
Set-Location -LiteralPath '.\zentao-codex-workflow'
Copy-Item -LiteralPath '.\settings.example.json' -Destination '.\settings.json'
```

在 `settings.json` 中填写禅道地址、账号及实际仓库路径，并确认 `codex.command` 对应本机可用的 Codex CLI。示例使用 Windows 的 `codex.cmd app-server`；密码在启动后的“工作流设置”中保存。已有本地配置时直接使用，不要再次复制模板覆盖。

启动已有项目：

```powershell
Set-Location -LiteralPath 'D:\wkproj\zentao-codex-workflow'
.\launch.ps1
```

脚本启动本地服务并打开浏览器，默认地址为 **http://127.0.0.1:8765/**。需要只启动服务时运行 `py -3 -X utf8 app.py --no-browser`。端口在 `settings.json` 的 `web.port` 中明确配置，占用时会停止，不自动更换端口。

关闭网页不会终止已确认的后台任务；需要停止时，在“工作流设置”点击“暂停并停止本地服务”，或在启动终端按 Ctrl+C。会话会保存，服务重新启动后恢复查看，业务执行不会自动继续。

## 使用流程

1. **工作项与资料**：启动后尝试连接禅道一次，不自动重试登录。列表按类型和编号区分，支持筛选和搜索；明确选择一条工作项后读取正文、图片、附件及关联需求。
2. **仓库与分支**：勾选本次涉及的仓库，读取已有分支。选择现有分支，或自行填写新分支名称并选择基准。已有分支若被其他工作目录占用，Git 会拒绝，不自动替换分支。
3. **补充需求**：填写必要业务规则、验收标准或附件关键内容。开始计划时创建独立 worktree，不携带或清理原目录中的未提交修改。
4. **计划与会话**：原生问题逐项等待回答，没有预选答案或超时放行。生成最终计划后，明确点击“确认当前计划并实施”。
5. **Review**：实施完成后自动审查。问题区分本次引入、原有问题和证据不足，并展示依据、建议及业务影响。只修复选中项；默认最多三轮修复复审，加上首次 Review，最多四次审查。
6. **本地提交**：没有问题时直接提交；仍有问题时，须选中全部剩余问题并填写接受原因。达到轮次上限仍有问题时暂停，可明确追加一轮。

“查看累计代码差异”包含已有文件差异和新增文本文件内容。每个仓库分别记录 commit；部分提交失败会保留已成功的结果，不自动回滚或重复提交。不 push、不修改禅道状态。

## 刷新与历史

- 浏览器刷新后重新加载当前工作项、阶段、计划、待回答问题、Review 和提交记录。
- 已输入但未提交的补充说明、问题答案、仓库选择和 Review 选择保存在当前浏览器会话草稿中。密码不进入草稿。
- 实时输出通过 SSE 推送，连接中断后恢复事件流；服务重启后需刷新页面重新连接。
- 服务重启时恢复上次打开的会话；原本正在执行的阶段显示为已暂停，须人工继续。
- 原桌面客户端的 JSON 会话、附件及 Windows 加密凭据继续使用，不要求重新配置账号。

## 配置与凭据

`settings.json` 是唯一的本地运行配置文件，保存禅道地址、账号、仓库路径、Codex 命令、默认模型和修复轮次，已加入 Git 忽略规则。公开仓库只维护脱敏模板 `settings.example.json`。分支没有默认值，必须在页面人工确定。

`prompts/review.md` 保存用户提供的固定 Review 原文。流程开始后锁定配置；流程内若提示词被外部改写，会明确停止。

密码仅通过 Windows 当前用户 DPAPI 加密保存在 `.local/zentao-password.bin`。页面不回显密码，服务不向前端返回禅道 Token。`.local/` 和 `work/` 已加入忽略列表，包含业务资料或检查中间文件，不应上传或提交。

## 后续开发与推送

直接在当前目录开发、提交和推送，无需同步到其他副本。本地 `settings.json`、加密凭据及会话资料继续使用，Git 更新不覆盖它们。修改配置结构时同步更新 `settings.example.json`，使用者再按模板手动调整本地配置。

```powershell
git diff
git add README.md
git commit -m "docs(workflow): 完善工作流使用说明"
git push origin main
```

上面的 `git add` 路径按本次实际修改文件替换。提交采用 `type(scope): 中文说明`，详细约定见 [贡献与提交规范](CONTRIBUTING.md)。应用自身只进行本地提交；向 GitHub 推送由使用者明确操作。

## 页面与本地接口

前端使用 AIS 项目已有的 React、Ant Design 和 dayjs 发布资源，采用相同的主题色、按钮、表格、表单及弹窗组件体系。资源已复制到 `web/dist/vendor/`，版本记录和许可证一并保留，页面不访问外部 CDN，不需要构建。

本地 HTTP 服务提供工作流操作、分支读取、差异查看、受限附件访问及 SSE。操作要求本机来源、会话 Cookie、独立操作凭据及明确请求标识；重复请求不会重复执行。计划确认、修复选择及提交还检查页面所见的流程版本。

核心仍由 `workflow/engine.py`、`rpc.py`、`mcp_server.py`、`gitops.py` 等模块负责。原生只读沙箱与受控 MCP 文件工具继续使用；文件写入只在已确认的实施、修复阶段授权。

## 支持范围

- 禅道 API v1：账号密码登录、当前用户、分页待办、详情、关联需求 / 来源 Bug、附件及正文图片。
- 文本提取：UTF-8 文本及不含内嵌媒体的 DOCX、PPTX。PDF、电子表格、扫描件、旧版 Office、压缩包、加密文档及含内嵌媒体的文件明确提示人工补充。
- 飞书第一版不读取，只保留链接；必要规则由人工补充。
- “需求”对应研发需求 story，另有任务 task、Bug bug；不混入用户需求 requirement 或业务需求 epic。
- Java 代码仅做轻量静态检查，不执行编译、构建或运行验证。网页迁移没有实施任何真实业务需求，业务功能最终仍需人工验收。

Codex 官方协议参考：[App Server](https://learn.chatgpt.com/docs/app-server)、[功能配置](https://learn.chatgpt.com/docs/config-file/config-basic)。
