"""评测标注：语料文件见本目录，硬约束 C2：仓库内不含真实业务资料。

- 自制部分：内部制度类文档、FAQ、英文制度、DOCX/PDF（运行期生成，内容见本文件常量）。
- 抓取部分：五部法律法规（维基文库，官方文件不受著作权保护），由 scripts/fetch_corpus.py 生成。

每条命中用例：问题 → 预期片段（`any_of` 命中其一即可），并断言命中分块属于 `doc` 指定文档。
负面问题（语料中无答案）应返回 no_evidence。

注意：检索当前使用本地确定性 HashEmbedder（字符二元组），语义改写能力有限，
问题措辞与原文保持一定词汇重叠；换真实向量模型后问题集与阈值需重新标定。
"""

from __future__ import annotations

HIT_CASES: list[dict[str, object]] = [
    # ---- 行政：会议室（MD，自制） ----
    {"doc": "meeting", "question": "会议室使用后需要完成哪些事项", "any_of": ["清理现场", "关闭"]},
    {"doc": "meeting", "question": "借用会议室要提前多久登记", "any_of": ["提前一天"]},
    {"doc": "meeting", "question": "会议室里禁止携带哪些东西", "any_of": ["食品"]},
    # ---- 行政：公务车辆（TXT，自制） ----
    {"doc": "vehicle", "question": "公务用车要提前多久提交申请", "any_of": ["提前半天"]},
    {"doc": "vehicle", "question": "车辆使用完毕应停放到哪里", "any_of": ["地库 B2 层"]},
    {"doc": "vehicle", "question": "过路费和停车费什么时候报销", "any_of": ["下月五日前"]},
    # ---- 财务：差旅报销（MD，自制） ----
    {"doc": "travel", "question": "省会城市住宿费上限是多少钱一晚", "any_of": ["350 元"]},
    {"doc": "travel", "question": "出差返回后报销单要在几天内提交", "any_of": ["10 个工作日"]},
    {"doc": "travel", "question": "编号 KS-FIN-012 规定了什么", "any_of": ["下月批次"], "kind": "keyword"},
    # ---- 信息：账号密码（MD，自制） ----
    {"doc": "security", "question": "编号 KS-SEC-003 的条款内容", "any_of": ["堡垒机"], "kind": "keyword"},
    {"doc": "security", "question": "系统密码长度最少要多少位", "any_of": ["12 位"]},
    {"doc": "security", "question": "密码多久需要更换一次", "any_of": ["90 天"]},
    {"doc": "security", "question": "生产环境操作必须经过什么设备", "any_of": ["堡垒机"]},
    # ---- 劳动法（MD，抓取） ----
    {"doc": "labor_law", "question": "劳动者平均每周工作时间不超过多少小时", "any_of": ["四十四小时"]},
    {"doc": "labor_law", "question": "工资分配应当遵循什么原则", "any_of": ["同工同酬"]},
    {"doc": "labor_law", "question": "法定休假日安排劳动者工作支付不低于多少工资报酬",
     "any_of": ["百分之三百"]},
    # ---- 劳动合同法（MD，抓取） ----
    {"doc": "labor_contract", "question": "劳动合同期限一年以上不满三年的试用期最长多久",
     "any_of": ["二个月"]},
    {"doc": "labor_contract", "question": "三年以上固定期限劳动合同的试用期不得超过多久",
     "any_of": ["六个月"]},
    # ---- 数据安全法（MD，抓取） ----
    {"doc": "data_sec", "question": "国家建立什么制度对数据实行分类分级保护",
     "any_of": ["数据分类分级保护制度"]},
    # ---- 网络安全法（MD，抓取） ----
    {"doc": "cyber_sec", "question": "网络安全等级保护制度要求网络运营者履行什么义务",
     "any_of": ["等级保护"]},
    # ---- 住房公积金管理条例（MD，抓取） ----
    {"doc": "housing_fund", "question": "住房公积金缴存比例不得低于多少",
     "any_of": ["5％", "5%"]},
    # ---- 新员工 FAQ（TXT，自制：问答同段成块） ----
    {"doc": "onboard_faq", "question": "入职当天需要携带哪些材料", "any_of": ["身份证原件"]},
    {"doc": "onboard_faq", "question": "工资每个月几号发放", "any_of": ["十五日"]},
    {"doc": "onboard_faq", "question": "年假有几天", "any_of": ["五天带薪年假"]},
    {"doc": "onboard_faq", "question": "内部系统账号由谁开通", "any_of": ["信息科"]},
    {"doc": "onboard_faq", "question": "请假一天以内向谁申请", "any_of": ["直属主管"]},
    # ---- 英文远程办公制度（MD，自制：跨语种命中） ----
    {"doc": "remote_en", "question": "how many remote work days per week",
     "any_of": ["two remote-work days"]},
    {"doc": "remote_en", "question": "remote work allowance per month", "any_of": ["300 yuan"]},
    # ---- 格式：DOCX（运行期生成） ----
    {"doc": "backup_docx", "question": "核心业务数据多久做一次全量备份", "any_of": ["每日凌晨"],
     "kind": "docx"},
    {"doc": "backup_docx", "question": "备份磁带出入库有什么要求", "any_of": ["双人复核"],
     "kind": "docx"},
    # ---- 格式：PDF（运行期生成，页码期望见 test_effectiveness） ----
    {"doc": "visitor_pdf", "question": "访客进入办公区需要谁陪同", "any_of": ["接待人"],
     "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "访客证的有效期是多久", "any_of": ["当日有效"],
     "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "外来车辆应该停放在哪里", "any_of": ["访客专区"],
     "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "消防通道可以堆放物品吗", "any_of": ["不得堆放"],
     "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "末班班车几点发车", "any_of": ["十点半"], "kind": "pdf"},
    {"doc": "visitor_pdf", "question": "监控录像保存多长时间", "any_of": ["九十天"], "kind": "pdf"},
]

NEGATIVE_QUESTIONS: list[str] = [
    "育儿假有多少天",
    "护照丢失了怎么补办",
    "公司食堂午餐几点开始供应",
    "美国的医疗保险怎么办理",
    "2026 年世界杯在哪里举办",
]

# 文档文件名 → 语料键（自制文档 + 抓取的法律法规）
CORPUS_FILES: dict[str, str] = {
    "meeting": "行政-会议室管理办法.md",
    "vehicle": "行政-公务车辆使用规定.txt",
    "travel": "财务-差旅费报销标准.md",
    "security": "信息-账号与密码管理规范.md",
    "labor_law": "法律-劳动法.md",
    "labor_contract": "法律-劳动合同法.md",
    "data_sec": "法律-数据安全法.md",
    "cyber_sec": "法律-网络安全法.md",
    "housing_fund": "法规-住房公积金管理条例.md",
    "onboard_faq": "faq-新员工入职常见问题.txt",
    "remote_en": "en-remote-work-policy.md",
}

# 运行期生成的 DOCX / PDF 内容（与断言配套，勿改其一不改其二）
DOCX_PARAGRAPHS: list[str] = [
    "《数据备份管理细则（测试资料）》",
    "（本文件为自制评测语料，内容虚构。）",
    "第一条 核心业务数据每日凌晨执行一次全量备份。",
    "第二条 备份文件保留 30 天，并在异地存放一份。",
    "第三条 每季度组织一次恢复演练，并留存演练记录。",
    "第四条 恢复演练未达标的系统须在两周内整改并复测。",
    "第五条 备份磁带出入库须双人复核并登记台账。",
]

PDF_PAGES: list[list[str]] = [
    [
        "《办公区管理规程（测试资料）》",
        "（本文件为自制评测语料，内容虚构。）",
        "第一条 所有访客须在前台登记并领取访客证。",
        "第二条 接待人须全程陪同访客进入办公区。",
    ],
    [
        "第三条 访客证当日有效，离开时交回前台。",
        "第四条 外来车辆须提前报备车牌号，停放在访客专区。",
    ],
    [
        "第五条 消防通道内不得堆放任何物品，安全出口指示灯须保持常亮。",
        "第六条 办公区每季度举行一次消防演练，全员必须参加。",
    ],
    [
        "第七条 加班超过晚上十点的员工，可乘坐末班班车并在前台登记。",
        "第八条 末班班车发车时间为晚上十点半，路线覆盖主要居住区。",
    ],
    [
        "第九条 失物招领处设在一层前台，物品保留三十天。",
        "第十条 监控录像保存九十天，调阅须经行政部审批。",
    ],
]
