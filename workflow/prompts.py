"""阶段提示只保留本轮职责；稳定规则由线程技能及工具 Schema 提供。"""

PLAN = "以原生计划模式分析本次需求，只输出可实施的中文 <proposed_plan>。资料不足先提问，计划阶段不修改代码。"
FOLLOWUP_PLAN = "按 followup 规划本轮需求；未完成任务结合 continuation_context，保留已有代码和剩余目标；已完成交付仅作背景。"
FOLLOWUP = (
    "按 followup 实际意图处理：查询只读，明确修改才实施对应范围；未完成任务结合 continuation_context。"
    "计划模式已关闭，不自行切换；范围变化先提问。本轮首次修改不计入复审次数。"
    "显式审查或提交要求调用 request_review 登记，引用原话并保留人工标题；不要自行审查或提交。"
)
IMPLEMENT = "按控制器已授权计划实施最小改动，先阅读现有代码与项目规范。通过 MCP 文件工具修改，只做静态检查，不执行命令或提交。"
REVIEW = (
    "只读审查，先用 changes 核对本轮片段，再按需阅读基线、当前代码及调用链，覆盖新增文件和上轮修复。"
    "按 Schema 返回 JSON，问题须有需求、代码证据、建议和影响；证据不足如实说明。"
    "旧问题沿用 previous_id，解决项在 resolved_previous 给出代码依据，遗漏不算解决。"
    "commit_scope 核对需求、初始差异和修改记录；来源仅供参考，其他会话或归属重叠的相关片段经审查可纳入，排除无关改动；范围不明确才提问。"
    "后端编译检查结果见 compile_checks，前端仍须审查，未检查不算通过，不声称运行验收完成。"
)
FIX = "只修复以下选中问题，保留未选项和无关原有问题。超出授权先提问；完成后由控制器复审，不自行提交。"
RESTORE = "先核对 task_context 和当前代码，按原阶段继续；确认中断前实际状态，不跳过待答问题或计划授权。"
COMPILE_FIX = (
    "按最新 compile_checks 的 repair_repository/repair_paths 修复本轮相关错误及已确认受影响调用方。"
    "结合基线、差异和诊断核实因果，范围内技术修复直接处理；不改依赖、环境及无关原有问题。"
    "必要业务资料缺失或需扩大范围才提问；完成后控制器复查，不执行命令、不自行审查提交。"
)
COMPILE_ANALYSIS = (
    "只读分析最新 compile_checks，结合 compiler_diagnostics、failure_reasons、task_paths、changes、基线和源码核实因果。"
    "报错行未修改不代表无关，须检查本轮签名、类型、字段、导入变更对调用方的影响。"
    "有任务因果和源码证据时用 task_code；repairs 限诊断或任务文件，逐字提供 source_excerpt、related_path 和 reason。"
    "原有问题、下载依赖、环境、证据不足分别用 original_code/dependencies/environment/unknown，repairs 为空。"
    "不修改文件、不执行命令或安装依赖。按 Schema 返回 JSON，summary 用中文。"
)
COMPILE_MANUAL_ANALYSIS = (
    "只读分析最新 compile_checks 的 analysis_repository、failure_reasons 和 diagnostics，结合差异及源码说明具体原因和处理办法。"
    "区分本轮引入、原有和证据不足；符号缺失不能直接认定要下载依赖，历史诊断不足须说明并建议重新检查。"
    "不修改、不执行命令或安装依赖；需要资料时提问，否则中文回答。结束后等待人工选择，不自动 Review 或提交。"
)
