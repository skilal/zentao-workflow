"""执行端与只读目录共用的固定组装模板，不包含任何会话资料。"""

SCOPE_CONTINUATION = "本次续接未完成任务，结合 continuation_context、原需求和 followup 消息完成本轮剩余工作；已有代码保留。"
SCOPE_FOLLOWUP = (
    "原需求资料与历史计划用于理解业务背景，本轮处理 followup 消息及当前回合内的补充说明。"
    "获取信息时只读取并回答；明确要求修改时才处理对应修改。是否自动 Review 由控制器决定。"
)
SCOPE_CONVERSATION = "本次未关联禅道工作项，以 supplement 中的首条消息和 supplement_images 为需求资料，按当前阶段和控制器授权处理。"
SCOPE_ITEMS = "本次选中的所有工作项属于同一次交付，须逐项分析并统一规划前后端实现，不得遗漏任务。"

MATERIAL_CHANGED = "资料首次读取或版本已变更，先调用 workflow.task_context(include_materials=true)。"
MATERIAL_CACHED = "先调用 workflow.task_context 获取当前授权；原需求资料沿用本线程，需核对时再设置 include_materials=true。"
MATERIAL_NAMING = (
    "先调用 workflow.task_context(include_materials=true)。已关联工作项时结合资料概括首个工作项；"
    "未关联工作项时，根据 supplement 中的首条需求消息及 supplement_images 概括实际需求。"
    "用 workflow.set_session_title(title) 登记中文名称，建议 6～12 字、最多 {max_length} 字符，"
    "不带类型、编号、引号或解释，多任务后缀由控制器追加。随后继续计划；命名失败不阻塞、不增加回合或循环重试。"
)
TURN = (
    "当前阶段：{phase}。{scope}\n{text}\n{policy_instruction}{material_instruction}"
    "\n结合当前回合内的用户补充说明调整处理重点，遵守本阶段职责、输出要求和现有授权。涉及授权变化时按原流程提问。"
    "\n工作流上下文：\n{context}"
)
BRANCH_INPUT = "依据线程中的分支规则分析上下文，按 Schema 返回。\n{context}"
REVIEW_INPUT = "{prompt}\n{review}\n上轮审查：{previous}"
REVIEW_IDS = (
    "\n第 {delivery_round} 轮交付，可引用旧编号：{previous_ids}"
    "；previous_id 仅限以上编号或空字符串，resolved_previous 仅列有证据的解决项。其他交付的问题仅作背景。"
)
FIX_INPUT = "{fix}\n{findings}"
ANSWER_INPUT = (
    "用户已补充信息：\n{reply}"
    "\n请从上次暂停点继续；若在计划阶段，输出最终 proposed_plan，由控制器判断是否需要人工确认。"
)
