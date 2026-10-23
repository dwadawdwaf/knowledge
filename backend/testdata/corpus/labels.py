"""评测标注（自制语料，硬约束 C2：不含真实资料）。

每条用例：问题 → 预期命中片段（`any_of` 命中其一即可）。
`kind` 说明断言路径：keyword（文号/专名，关键词路为主）、fact（事实型提问）、
format（依赖运行期生成的 DOCX/PDF，另带 locator 期望）。
负面问题（语料中无答案，应返回 no_evidence）单列。

注意：检索当前使用本地确定性 HashEmbedder（字符二元组），语义改写能力有限，
问题的措辞与原文保持一定词汇重叠；换真实向量模型后问题集与阈值需重新标定。
"""

from __future__ import annotations

HIT_CASES: list[dict[str, object]] = [
    # ---- 行政：会议室（MD） ----
    {"doc": "meeting", "question": "会议室使用后需要完成哪些事项",
     "any_of": ["清理现场", "关闭"]},
    {"doc": "meeting", "question": "借用会议室要提前多久登记",
     "any_of": ["提前一天"]},
    {"doc": "meeting", "question": "会议室里禁止携带哪些东西",
     "any_of": ["食品"]},
    # ---- 行政：公务车辆（TXT） ----
    {"doc": "vehicle", "question": "公务用车要提前多久提交申请",
     "any_of": ["提前半天"]},
    {"doc": "vehicle", "question": "车辆使用完毕应停放到哪里",
     "any_of": ["地库 B2 层"]},
    {"doc": "vehicle", "question": "过路费和停车费什么时候报销",
     "any_of": ["下月五日前"]},
    # ---- 财务：差旅报销（MD） ----
    {"doc": "travel", "question": "省会城市住宿费上限是多少钱一晚",
     "any_of": ["350 元"]},
    {"doc": "travel", "question": "出差返回后报销单要在几天内提交",
     "any_of": ["10 个工作日"]},
    {"doc": "travel", "question": "编号 KS-FIN-012 规定了什么",
     "any_of": ["下月批次"], "kind": "keyword"},
    # ---- 信息：账号密码（MD） ----
    {"doc": "security", "question": "编号 KS-SEC-003 的条款内容",
     "any_of": ["堡垒机"], "kind": "keyword"},
    {"doc": "security", "question": "系统密码长度最少要多少位",
     "any_of": ["12 位"]},
    {"doc": "security", "question": "密码多久需要更换一次",
     "any_of": ["90 天"]},
    {"doc": "security", "question": "生产环境操作必须经过什么设备",
     "any_of": ["堡垒机"]},
    # ---- 格式：DOCX（运行期生成） ----
    {"doc": "backup_docx", "question": "核心业务数据多久做一次全量备份",
     "any_of": ["每日凌晨"], "kind": "docx"},
    # ---- 格式：PDF（运行期生成，页码期望见 test_effectiveness） ----
    {"doc": "visitor_pdf", "question": "访客进入办公区需要谁陪同",
     "any_of": ["接待人"], "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "访客证的有效期是多久",
     "any_of": ["当日有效"], "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "外来车辆应该停放在哪里",
     "any_of": ["访客专区"], "kind": "pdf"},
]

NEGATIVE_QUESTIONS: list[str] = [
    "年假可以折算成工资吗",
    "公积金提取需要准备什么材料",
    "公司食堂午餐几点开始供应",
]

# 文档文件名 → 语料键
CORPUS_FILES: dict[str, str] = {
    "meeting": "行政-会议室管理办法.md",
    "vehicle": "行政-公务车辆使用规定.txt",
    "travel": "财务-差旅费报销标准.md",
    "security": "信息-账号与密码管理规范.md",
}

# 运行期生成的 DOCX / PDF 内容（与断言配套，勿改其一不改其二）
DOCX_PARAGRAPHS: list[str] = [
    "《数据备份管理细则（测试资料）》",
    "（本文件为自制评测语料，内容虚构。）",
    "第一条 核心业务数据每日凌晨执行一次全量备份。",
    "第二条 备份文件保留 30 天，并在异地存放一份。",
    "第三条 每季度组织一次恢复演练，并留存演练记录。",
]

PDF_PAGES: list[list[str]] = [
    [
        "《访客管理规程（测试资料）》",
        "（本文件为自制评测语料，内容虚构。）",
        "第一条 所有访客须在前台登记并领取访客证。",
        "第二条 接待人须全程陪同访客进入办公区。",
    ],
    [
        "第三条 访客证当日有效，离开时交回前台。",
        "第四条 外来车辆须提前报备车牌号，停放在访客专区。",
    ],
]
