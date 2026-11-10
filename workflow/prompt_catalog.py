"""只读提示词目录；原文来自执行端常量与受控配置文件。"""

from workflow import prompt_templates as templates, prompts
from workflow.branching import BRANCH_INSTRUCTIONS
from workflow.change_locations import EDIT_PHASES, LOCATION_INSTRUCTIONS
from workflow.commit_messages import COMMIT_INSTRUCTIONS
from workflow.settings import ROOT, WorkflowError


def prompt_catalog(settings):
    skill_source = "skills/zentao-delivery/SKILL.md"
    skill = (ROOT / skill_source).read_text(encoding="utf-8")
    review_source = settings["review"]["prompt_file"]
    review = (ROOT / review_source).read_text(encoding="utf-8").strip()
    if not skill.strip() or not review:
        raise WorkflowError("通用规则或 Review 提示词为空，无法展示完整目录。")

    def fragment(key, title, content, source, condition):
        return {"id": key, "title": title, "content": content, "source": source, "condition": condition}

    scope_fragments = [
        fragment("scope_items", "选中工作项", templates.SCOPE_ITEMS, "workflow/prompt_templates.py · SCOPE_ITEMS", "已关联工作项，且不是续聊"),
        fragment("scope_conversation", "直接输入需求", templates.SCOPE_CONVERSATION, "workflow/prompt_templates.py · SCOPE_CONVERSATION", "未关联工作项，且不是续聊"),
        fragment("scope_followup", "已完成任务的续聊", templates.SCOPE_FOLLOWUP, "workflow/prompt_templates.py · SCOPE_FOLLOWUP", "有 followup，且不是续接未完成任务"),
        fragment("scope_continuation", "续接未完成任务", templates.SCOPE_CONTINUATION, "workflow/prompt_templates.py · SCOPE_CONTINUATION", "followup.in_progress 为真，优先于其他范围说明")]
    material_fragments = [
        fragment("material_changed", "首次读取或资料变更", templates.MATERIAL_CHANGED, "workflow/prompt_templates.py · MATERIAL_CHANGED", "变化上下文包含 materials_revision，且无需自动命名"),
        fragment("material_cached", "复用已读资料", templates.MATERIAL_CACHED, "workflow/prompt_templates.py · MATERIAL_CACHED", "资料版本未变，且无需自动命名"),
        fragment("material_naming", "计划回合自动命名", templates.MATERIAL_NAMING, "workflow/prompt_templates.py · MATERIAL_NAMING", "计划阶段有待生成的首次工作项会话名称时，替代其他资料读取指令")]
    review_fragments = [
        fragment("review_ids", "审查编号约束", templates.REVIEW_IDS, "workflow/prompt_templates.py · REVIEW_IDS", "所有 Review 阶段回合，包括恢复和回答继续"),
        fragment("review_skipped", "本轮不修复项", templates.REVIEW_SKIPPED, "workflow/prompt_templates.py · REVIEW_SKIPPED", "所有 Review 阶段回合，列表可为空"),
        fragment("commit_instructions", "提交消息规则", COMMIT_INSTRUCTIONS, "workflow/commit_messages.py · COMMIT_INSTRUCTIONS", "Review 阶段生成提交消息，由控制器执行 Git 提交")]
    location = fragment("locations", "修改位置登记", LOCATION_INSTRUCTIONS, "workflow/change_locations.py · LOCATION_INSTRUCTIONS",
                        "原阶段为 implement、followup、fix、compile_fix；纯问答无需登记")

    def turn(phase, text):
        if phase in EDIT_PHASES:
            text += "\n" + LOCATION_INSTRUCTIONS
        if phase == "review":
            text += templates.REVIEW_IDS.format(delivery_round="【交付轮次】", previous_ids="【可引用旧编号 JSON】")
            text += templates.REVIEW_SKIPPED.format(skipped_ids="【本轮不修复编号 JSON】")
            text += "\n" + COMMIT_INSTRUCTIONS
        return templates.TURN.format(phase=phase, scope="【需求范围说明：按条件选择一项】", text=text,
                                     material_instruction="【资料读取指令：按条件选择一项】", context="【变化上下文 JSON】")

    def variant(key, title, phase, text, condition):
        return {"id": key, "title": title, "layer": "回合级输入", "condition": condition, "content": turn(phase, text)}

    stages = []

    def stage(key, title, description, variants, fragments, *, controller=False, notes=""):
        stages.append({"id": key, "title": title, "description": description, "controller": controller,
                       "thread_rule": "分支专用规则" if key == "branch" else "通用工作流规则",
                       "templates": variants, "fragments": fragments, "notes": notes})

    stage("collect", "资料读取", "读取人工选择工作项的正文、图片及附件。", [], [], controller=True,
          notes="由控制器执行，无独立模型提示词。模型回合中的资料读取指令见通用规则。")
    stage("branch", "分支识别", "专用线程只分析提供的项目资料与分支元数据。", [
        {"id": "branch_thread", "title": "分支线程指令", "layer": "线程级指令", "condition": "新建或恢复分支识别线程",
         "content": BRANCH_INSTRUCTIONS},
        {"id": "branch_input", "title": "分支识别输入", "layer": "回合级输入", "condition": "待识别仓库存在时",
         "content": templates.BRANCH_INPUT.format(context="【分支上下文 JSON】")}], [
             fragment("branch_rules", "分支判断规则", BRANCH_INSTRUCTIONS, "workflow/branching.py · BRANCH_INSTRUCTIONS", "仅分支识别线程"),
             fragment("branch_input", "实际发送格式", templates.BRANCH_INPUT, "workflow/prompt_templates.py · BRANCH_INPUT", "分支输入替代通用回合包装，不发送控制器记录中的阶段提示")])
    stage("prepare", "工作区准备", "按确认的分支分配与准备前后端工作目录。", [], [], controller=True,
          notes="由控制器执行，无独立模型提示词；需要 AI 判断分支时进入分支识别阶段。")
    stage("plan", "需求分析与计划", "使用原生计划模式分析需求；首次工作项会话可同时生成名称。", [
        variant("plan", "首次需求计划", "plan", prompts.PLAN, "首次进入需求计划"),
        variant("followup_plan", "续聊计划模式", "plan", prompts.PLAN + prompts.FOLLOWUP_PLAN, "续聊选择计划模式时")], [
            fragment("plan", "需求计划指令", prompts.PLAN, "workflow/prompts.py · PLAN", "首次计划与续聊计划"),
            fragment("followup_plan", "续聊计划补充", prompts.FOLLOWUP_PLAN, "workflow/prompts.py · FOLLOWUP_PLAN", "续聊计划模式，紧接 PLAN 拼接，无额外分隔符"),
            *scope_fragments, *material_fragments])
    stage("implement", "实施修改", "按已授权计划实施；只做静态检查，实际修改后登记入口位置。", [
        variant("implement", "授权后实施", "implement", prompts.IMPLEMENT, "计划经控制器授权后")], [
            fragment("implement", "实施指令", prompts.IMPLEMENT, "workflow/prompts.py · IMPLEMENT", "实施阶段"), location,
            *scope_fragments, *material_fragments])
    stage("compile", "编译检查与分析", "实际后端编译由控制器执行；失败分析使用以下两种互斥入口。", [
        variant("automatic", "自动分析编译失败", "compile_analysis", prompts.COMPILE_ANALYSIS, "控制器自动分析失败，按 Schema 返回 JSON"),
        variant("manual", "人工选择分析原因", "compile_analysis", prompts.COMPILE_MANUAL_ANALYSIS, "人工选择分析失败原因，中文回答后等待人工处理")], [
            fragment("automatic", "自动分析指令", prompts.COMPILE_ANALYSIS, "workflow/prompts.py · COMPILE_ANALYSIS", "自动分析入口"),
            fragment("manual", "人工分析指令", prompts.COMPILE_MANUAL_ANALYSIS, "workflow/prompts.py · COMPILE_MANUAL_ANALYSIS", "人工分析入口"),
            *scope_fragments, *material_fragments], controller=True,
          notes="实际编译由控制器执行，无独立模型提示词；编译诊断通过变化上下文中的 compile_checks 提供。查看本页不会启动编译。")
    stage("compile_fix", "编译问题修复", "仅修复已确认与本轮任务有关的诊断与受影响调用方。", [
        variant("compile_fix", "编译问题修复", "compile_fix", prompts.COMPILE_FIX, "控制器确认任务相关修复范围后")], [
            fragment("compile_fix", "编译修复指令", prompts.COMPILE_FIX, "workflow/prompts.py · COMPILE_FIX", "编译修复阶段"), location,
            *scope_fragments, *material_fragments])
    stage("review", "Review 审查", "组合当前 Review 文件原文、审查职责、历史结果、编号约束和提交规则。", [
        variant("review", "开始或重新 Review", "review", templates.REVIEW_INPUT.format(
            prompt=review, review=prompts.REVIEW, previous="【上轮审查结果 JSON；首次为 null】"), "开始或重新审查时")], [
            fragment("review_file", "当前 Review 文件原文", review, review_source, "新会话采用当前配置；已有会话可能沿用保存的审查快照"),
            fragment("review", "审查阶段职责", prompts.REVIEW, "workflow/prompts.py · REVIEW", "开始或重新 Review"),
            fragment("review_input", "审查输入拼接", templates.REVIEW_INPUT, "workflow/prompt_templates.py · REVIEW_INPUT", "固定文件原文、审查职责、上轮审查依次拼接"),
            *review_fragments, *scope_fragments, *material_fragments],
          notes="这里显示当前配置文件内容，不代表某个已有会话实际使用的审查快照。原“工作流设置”中的 Review 编辑入口保持不变。")
    stage("fix", "问题修复", "只修复人工选中的问题，随后由控制器复审。", [
        variant("fix", "修复选中问题", "fix", templates.FIX_INPUT.format(
            fix=prompts.FIX, chosen="【选中问题 JSON】", note="【人工修复说明】"), "人工选择问题并点击修复后")], [
            fragment("fix", "修复职责", prompts.FIX, "workflow/prompts.py · FIX", "修复选中的问题"),
            fragment("fix_input", "修复输入拼接", templates.FIX_INPUT, "workflow/prompt_templates.py · FIX_INPUT", "只注入选中问题及人工说明"),
            location, *scope_fragments, *material_fragments])
    stage("commit", "本地提交", "审查通过且满足提交条件后，由控制器提交确认的代码片段。", [], [review_fragments[-1]], controller=True,
          notes="由控制器执行，无独立模型提示词。提交消息由 Review 阶段生成；不推送远程仓库。")
    stage("followup", "后续对话", "按续聊意图只读回答或处理授权修改。", [
        variant("followup", "非计划模式续聊", "followup", prompts.FOLLOWUP, "续聊未选择计划模式；是否自动 Review 由控制器决定")], [
            fragment("followup", "续聊指令", prompts.FOLLOWUP, "workflow/prompts.py · FOLLOWUP", "非计划模式续聊"),
            location, *scope_fragments, *material_fragments], notes="续聊选择计划模式时使用“需求分析与计划”中的续聊模板。")
    phases = [("plan", "计划"), ("implement", "实施"), ("review", "Review"), ("fix", "问题修复"),
              ("followup", "后续对话"), ("compile_analysis", "编译分析"), ("compile_fix", "编译修复")]
    stage("restore", "暂停恢复", "继续原阶段；只有真正启动模型回合时使用此模板。", [
        variant(phase, "恢复" + title, phase, prompts.RESTORE, "中断前的阶段为“" + title + "”") for phase, title in phases], [
            fragment("restore", "恢复指令", prompts.RESTORE, "workflow/prompts.py · RESTORE", "继续原阶段的模型回合"),
            location, *review_fragments, *scope_fragments, *material_fragments],
          notes="恢复分支识别、准备工作区或受控编译时，由控制器回到相应流程，不强行发送此模板。")
    stage("answer", "回答后继续", "将人工答复带回原阶段，保留该阶段适用的附加规则。", [
        variant(phase, "继续" + title, phase, templates.ANSWER_INPUT.format(reply="【人工补充回答】"),
                "人工回答后回到“" + title + "”阶段") for phase, title in phases], [
            fragment("answer", "回答继续模板", templates.ANSWER_INPUT, "workflow/prompt_templates.py · ANSWER_INPUT", "原阶段继续模型回合"),
            location, *review_fragments, *scope_fragments, *material_fragments],
          notes="仍在运行的原生提问请求只回传结构化答案，不新增提示词；这里展示已结束回合后再次启动模型的格式。")
    stage("common", "通用规则", "线程级规则与回合包装共同约束各阶段；条件片段不会同时生效。", [
        {"id": "common_thread", "title": "通用工作流规则", "layer": "线程级指令", "condition": "非分支识别线程新建或恢复时",
         "content": skill},
        {"id": "common_turn", "title": "通用回合包装", "layer": "回合级输入", "condition": "非分支识别的模型回合",
         "content": templates.TURN}], [
             fragment("skill", "线程级通用规则原文", skill, skill_source, "非分支识别线程"),
             fragment("turn", "回合级组装格式", templates.TURN, "workflow/prompt_templates.py · TURN", "非分支识别模型回合"),
             *scope_fragments, *material_fragments, location, *review_fragments],
          notes="上下文只发送变化字段；图片以 localImage 输入按内容去重，不在文本模板中展开。输出 JSON Schema 和工具定义属于接口约束，不是提示词正文。本页不包含模型服务内部系统提示或业务仓库规则原文。")
    return {"stages": stages, "placeholders": [
        {"name": "【需求范围说明：按条件选择一项】", "description": "按续接未完成任务、普通续聊、直接需求、关联工作项的条件选择唯一范围说明。"},
        {"name": "【资料读取指令：按条件选择一项】", "description": "自动命名优先，否则按资料版本选择首次读取或复用资料。"},
        {"name": "【变化上下文 JSON】", "description": "动态需求、仓库、计划、授权决定、附件引用及 compile_checks（含诊断与修复范围）等，仅发送变化字段，不在本页读取实际值。"},
        {"name": "【分支上下文 JSON】", "description": "所属项目、工作项、补充说明、正文及待识别仓库的分支元数据。"},
        {"name": "【上轮审查结果 JSON；首次为 null】／【可引用旧编号 JSON】／【本轮不修复编号 JSON】／【交付轮次】", "description": "来自当前交付审查记录，不在本页读取或代入。"},
        {"name": "【选中问题 JSON】／【人工修复说明】／【人工补充回答】", "description": "来自人工选择和输入，不在本页读取或代入。"},
        {"name": "{phase}／{scope}／{text}／{material_instruction}／{context}", "description": "源码模板槽位，依次为阶段标识、唯一范围说明、阶段指令及附加规则、唯一资料读取指令、变化上下文 JSON。"},
        {"name": "{prompt}／{review}／{previous}", "description": "Review 文件原文（已有会话可采用快照）、审查职责、上轮审查 JSON；首次上轮结果为 null。"},
        {"name": "{fix}／{chosen}／{note}／{reply}", "description": "修复职责、选中问题 JSON、人工修复说明、人工补充回答。"},
        {"name": "{delivery_round}／{previous_ids}／{skipped_ids}", "description": "当前交付轮次、可引用旧问题编号 JSON、本轮不修复编号 JSON，编号列表可为空。"},
        {"name": "{max_length}", "description": "自动命名允许的标题长度，由控制器根据多任务后缀计算。"}],
        "description": "只读展示当前服务的固定原文与组装模板。占位符代表运行时数据，不是实际发送的会话内容。"}
