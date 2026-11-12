#!/usr/bin/env python3
"""生成 RAG 分块与检索基线的中英文多格式自制语料。

全部内容为虚构示例（公司、产品、人员、编号均不存在），不包含真实单位资料，
遵循仓库硬约束 C2。运行 `python testdata/rag-baseline-v1/generate.py` 重新生成
corpus/ 与 manifest.json；文本内容确定，容器格式可能写入生成时间，重新生成会更新哈希。
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import json
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from fpdf import FPDF
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from PIL import Image, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo
from pptx import Presentation
from pptx.util import Inches

ROOT = pathlib.Path(__file__).resolve().parent
CORPUS = ROOT / "corpus"
COMPANY = {"zh": "澄川科技", "en": "Chengchuan Labs"}
PRODUCTS = {
    "zh": ["澜图知识云", "汀澜检索", "白石存储", "青梧控制台", "砚舟网关"],
    "en": ["Lantu Knowledge Cloud", "Tinglan Retrieval", "Baishi Storage", "Qingwu Console", "Yanzhou Gateway"],
}
OWNERS = {
    "zh": ["林岚", "周泊", "陈棹", "顾青", "许澄"],
    "en": ["Mira Chen", "Owen Zhou", "Iris Gu", "Leo Xu", "Nina Holt"],
}
PLACES = {
    "zh": ["B3-07 库位", "A2-11 档案柜", "C1-03 资料室"],
    "en": ["Aisle B3-07", "Cabinet A2-11", "Records Room C1-03"],
}

FORMATS = ["md", "txt", "docx", "csv", "xlsx", "pptx", "pdf", "png"]

POLICY_TOPICS = [
    ("差旅与费用报销", "Travel and Expense Reimbursement", "travel-expense"),
    ("信息安全管理", "Information Security", "information-security"),
    ("会议室与公共空间", "Meeting Rooms and Shared Spaces", "meeting-rooms"),
    ("新员工入职与试用期", "Onboarding and Probation", "onboarding"),
    ("采购与供应商管理", "Procurement and Vendors", "procurement"),
    ("远程办公", "Remote Work", "remote-work"),
    ("培训与认证", "Training and Certification", "training"),
    ("固定资产管理", "Fixed Asset Management", "fixed-assets"),
    ("保密与对外交流", "Confidentiality and External Communication", "confidentiality"),
    ("应急与业务连续性", "Incident Response and Business Continuity", "continuity"),
]
TECH_TOPICS = [
    ("部署架构与网络分区", "Deployment Architecture and Network Zones", "deployment-architecture"),
    ("API 鉴权与密钥轮换", "API Authentication and Key Rotation", "api-authentication"),
    ("备份与恢复", "Backup and Restore", "backup-restore"),
    ("监控告警与值班", "Monitoring, Alerting, and On-call", "monitoring"),
    ("发布与回滚流程", "Release and Rollback", "release-rollback"),
    ("故障排查手册", "Troubleshooting Guide", "troubleshooting"),
    ("数据字典与字段约定", "Data Dictionary and Field Conventions", "data-dictionary"),
    ("容量规划与扩容", "Capacity Planning and Scaling", "capacity-planning"),
    ("日志规范与留存", "Logging Standards and Retention", "logging"),
    ("SDK 快速开始", "SDK Quick Start", "sdk-quickstart"),
]
FAQ_TOPICS = [
    ("计费与套餐", "Billing and Plans", "billing-plans"),
    ("发票与付款", "Invoices and Payments", "invoices"),
    ("权限与角色", "Permissions and Roles", "permissions"),
    ("第三方集成", "Third-party Integrations", "integrations"),
    ("数据导出", "Data Export", "data-export"),
    ("移动端使用", "Mobile App", "mobile-app"),
    ("通知设置", "Notification Settings", "notifications"),
    ("审计日志", "Audit Logs", "audit-logs"),
    ("服务等级协议", "Service Level Agreement", "service-level"),
    ("数据保留与删除", "Data Retention and Deletion", "retention-deletion"),
]
TOPICS = {"policy": POLICY_TOPICS, "tech": TECH_TOPICS, "faq": FAQ_TOPICS}
THEME_CYCLE = ["policy", "tech", "faq", "policy", "tech", "faq"]

# 每类文档的三条待检索事实，值由唯一序号生成，避免跨文档串答案。
ATTRS = {
    "policy": [
        {"zh": "提交时限", "en": "submission deadline", "q_zh": "出差或事项结束后最晚多久必须提交单据？", "q_en": "How soon after the trip or event must the forms be submitted?", "kind": "days"},
        {"zh": "审批上限", "en": "approval limit", "q_zh": "单笔费用最高可以审批多少金额？", "q_en": "What is the maximum amount that may be approved for a single item?", "kind": "money"},
        {"zh": "保留期限", "en": "retention period", "q_zh": "相关记录至少需要保存多久？", "q_en": "How long must the related records be retained?", "kind": "days_keep"},
    ],
    "tech": [
        {"zh": "默认超时", "en": "default timeout", "q_zh": "系统默认等待多长时间后判定超时？", "q_en": "How long does the system wait before timing out by default?", "kind": "seconds"},
        {"zh": "最大重试次数", "en": "maximum retry count", "q_zh": "失败后最多自动重试几次？", "q_en": "How many automatic retries are allowed after a failure?", "kind": "count"},
        {"zh": "批量大小", "en": "batch size", "q_zh": "每批最多处理多少条记录？", "q_en": "How many records are processed per batch at most?", "kind": "count"},
    ],
    "faq": [
        {"zh": "响应时限", "en": "response time", "q_zh": "工单提交后最晚多久必须首次响应？", "q_en": "How soon must a ticket receive its first response?", "kind": "hours"},
        {"zh": "退款期限", "en": "refund window", "q_zh": "付款后多少天内可以申请退款？", "q_en": "Within how many days after payment can a refund be requested?", "kind": "days"},
        {"zh": "单文件上限", "en": "single-file limit", "q_zh": "单个文件最大允许多大？", "q_en": "What is the maximum size of a single file?", "kind": "size"},
    ],
}

HEADINGS = {
    "policy": {
        "zh": ["一、目的与适用范围", "二、职责与分工", "三、执行要求", "四、记录、检查与例外", "五、附则"],
        "en": ["1. Purpose and Scope", "2. Roles and Responsibilities", "3. Requirements", "4. Records, Review, and Exceptions", "5. Final Provisions"],
    },
    "tech": {
        "zh": ["一、适用范围与前置条件", "二、架构与依赖", "三、配置步骤", "四、参数与验收", "五、回滚与支持"],
        "en": ["1. Scope and Prerequisites", "2. Architecture and Dependencies", "3. Configuration Steps", "4. Parameters and Acceptance", "5. Rollback and Support"],
    },
    "faq": {
        "zh": ["一、适用范围", "二、常见问题", "三、套餐与限制", "四、计费与账单", "五、支持渠道"],
        "en": ["1. Scope", "2. Frequently Asked Questions", "3. Plans and Limits", "4. Billing and Invoices", "5. Support Channels"],
    },
}
FACT_SECTION = {"policy": 2, "tech": 2, "faq": 1}
TABLE_SECTION = {"policy": 3, "tech": 3, "faq": 2}
TABLE_HEADERS = {
    "policy": {"zh": ["编号", "事项", "标准", "审批人", "备注"], "en": ["Code", "Item", "Standard", "Approver", "Note"]},
    "tech": {"zh": ["编号", "参数", "默认值", "允许范围", "说明"], "en": ["Code", "Parameter", "Default", "Allowed Range", "Note"]},
    "faq": {"zh": ["编号", "项目", "标准", "适用版本", "备注"], "en": ["Code", "Item", "Standard", "Applicable Plan", "Note"]},
}
LEDGER_HEADERS = {
    "zh": ["资产编号", "资产类别", "资产名称", "责任人", "存放位置", "数量", "原值(元)", "状态", "备注"],
    "en": ["Asset code", "Category", "Name", "Custodian", "Location", "Quantity", "Original value (CNY)", "Status", "Note"],
}
FILLERS = {
    "zh": [
        "相关岗位应在每次操作后核对系统记录与书面凭证，发现差异当日报送归口部门。",
        "跨部门协作事项应明确牵头人和完成时限，避免因职责不清造成延误。",
        "涉及外部供应商的，应在合同中约定保密、审计与退出条款，并保留履约证据。",
        "变更前后均应记录时间、操作人和影响范围，便于后续追溯与复盘。",
        "发现异常时应先控制影响，再按升级路径通知值班负责人，不得隐瞒或延迟上报。",
        "培训材料与操作手册应随流程变化同步更新，旧版本及时归档。",
        "审批记录应包含申请事由、金额、权限级别和最终结论，缺一不可。",
        "抽查样本应覆盖不同部门、不同时间段和不同业务类型，避免只看单一场景。",
        "对外提供的材料须经归口部门复核，涉及数据的部分还应完成脱敏检查。",
        "季度复盘应给出问题清单、责任人和完成日期，并在下一次例会上逐项关闭。",
    ],
    "en": [
        "Each team should reconcile system records with written evidence after every operation and report discrepancies the same day.",
        "Cross-team work must name an owner and a completion date so that unclear responsibilities do not cause delays.",
        "Contracts with external suppliers must cover confidentiality, audit rights, and exit terms, with performance evidence retained.",
        "Every change should record its time, operator, and impact so that it can be traced and reviewed later.",
        "When an anomaly appears, contain the impact first, then notify the on-call lead through the escalation path.",
        "Training material and operating manuals must be updated together with process changes, and old versions archived.",
        "Approval records must include the reason, amount, authority level, and final decision; none of these may be omitted.",
        "Sampling should cover different departments, periods, and business types rather than a single scenario.",
        "External materials require review by the owning team, and any data included must pass a de-identification check.",
        "The quarterly review should produce an issue list with owners and due dates, closed item by item at the next meeting.",
    ],
}

FONT_CANDIDATES = [
    pathlib.Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
    pathlib.Path("C:/Windows/Fonts/msyh.ttc"),
    pathlib.Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
]
PDF_CJK_CANDIDATES = [
    pathlib.Path("/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf"),
    pathlib.Path("C:/Windows/Fonts/simhei.ttf"),
    pathlib.Path("C:/Windows/Fonts/msyh.ttc"),
]
PDF_LATIN_CANDIDATES = [
    pathlib.Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
    pathlib.Path("C:/Windows/Fonts/arial.ttf"),
]


def first_existing(candidates: list[pathlib.Path]) -> pathlib.Path:
    for path in candidates:
        if path.exists():
            return path
    raise RuntimeError(f"找不到可用字体：{candidates}")


def font_path() -> pathlib.Path:
    for path in FONT_CANDIDATES:
        if path.exists():
            return path
    raise RuntimeError("找不到可用的中文字体")


@dataclass
class Fact:
    attr_zh: str
    attr_en: str
    value: str
    question_zh: str
    question_en: str
    kind: str


@dataclass
class DocSpec:
    id: str
    fmt: str
    lang: str
    theme: str
    title: str
    topic_zh: str
    topic_en: str
    slug: str
    code: str
    alias: str
    product: str
    subject: str
    facts: list[Fact]
    table_rows: list[list[str]]
    table_headers: list[str]
    gold_row: list[str] | None
    features: list[str] = field(default_factory=list)
    requires_mineru: bool = False
    extra: dict[str, Any] = field(default_factory=dict)


def value_for(kind: str, number: int, lang: str) -> str:
    if kind == "days":
        return f"{101 + number * 3} 个工作日" if lang == "zh" else f"{101 + number * 3} business days"
    if kind == "money":
        return f"人民币 {6000 + number * 137} 元" if lang == "zh" else f"CNY {6000 + number * 137}"
    if kind == "days_keep":
        return f"{365 + number * 11} 天" if lang == "zh" else f"{365 + number * 11} days"
    if kind == "seconds":
        return f"{31 + number * 7} 秒" if lang == "zh" else f"{31 + number * 7} seconds"
    if kind == "count":
        return f"{2 + number} 次" if lang == "zh" else f"{2 + number} times"
    if kind == "hours":
        return f"{3 + number} 小时" if lang == "zh" else f"{3 + number} hours"
    if kind == "size":
        return f"{20 + number} MB"
    raise ValueError(kind)


def build_docs() -> list[DocSpec]:
    docs: list[DocSpec] = []
    seq = 0
    for fi, fmt in enumerate(FORMATS):
        for lang in ("zh", "en"):
            for i in range(6):
                seq += 1
                theme = THEME_CYCLE[i]
                topic = TOPICS[theme][(fi * 2 + i * 3) % 10]
                topic_zh, topic_en, slug = topic
                code = f"CC-{fmt.upper()}-{lang.upper()}-{i + 1:02d}"
                alias = f"青砾-{100 + seq}" if lang == "zh" else f"ember-{100 + seq}"
                product = PRODUCTS[lang][seq % len(PRODUCTS[lang])]
                if theme == "policy":
                    subject = f"{alias}专项" if lang == "zh" else f"{alias} program"
                elif theme == "tech":
                    subject = f"{alias}服务" if lang == "zh" else f"{alias} service"
                else:
                    subject = f"{alias}套餐" if lang == "zh" else f"{alias} plan"
                attrs = ATTRS[theme]
                facts = [
                    Fact(
                        attr_zh=attrs[0]["zh"],
                        attr_en=attrs[0]["en"],
                        value=value_for(attrs[0]["kind"], seq * 2, lang),
                        question_zh=attrs[0]["q_zh"],
                        question_en=attrs[0]["q_en"],
                        kind=attrs[0]["kind"],
                    ),
                    Fact(
                        attr_zh=attrs[1]["zh"],
                        attr_en=attrs[1]["en"],
                        value=value_for(attrs[1]["kind"], seq * 2 + 1, lang),
                        question_zh=attrs[1]["q_zh"],
                        question_en=attrs[1]["q_en"],
                        kind=attrs[1]["kind"],
                    ),
                ]
                owner = OWNERS[lang][seq % len(OWNERS[lang])]
                place = PLACES[lang][seq % len(PLACES[lang])]
                row_code = f"{code}-R{17 + i:02d}"
                gold_row = [
                    row_code,
                    subject,
                    facts[0].value,
                    owner,
                    place,
                ]
                table_headers = TABLE_HEADERS[theme][lang]
                rows = [gold_row]
                for n in range(4):
                    rows.append([
                        f"{code}-R{40 + n:02d}",
                        f"{subject}（辅助 {n + 1}）" if lang == "zh" else f"{subject} (aux {n + 1})",
                        value_for(attrs[0]["kind"], seq * 10 + n + 1, lang),
                        OWNERS[lang][(seq + n + 1) % len(OWNERS[lang])],
                        f"仅适用于辅助记录 {n + 1}" if lang == "zh" else f"Applies only to auxiliary record {n + 1}",
                    ])
                features: list[str] = []
                requires_mineru = fmt == "png"
                if fmt == "md":
                    features = ["headings", "pipe-table", "long-section"]
                    if i in (0, 3):
                        features.append("code-fence")
                    if i in (1, 4):
                        features.append("skipped-level")
                    if i == 2:
                        features.extend(["duplicate-heading", "long-list"])
                    if i == 4:
                        features.append("front-matter")
                elif fmt == "txt":
                    features = ["plain-text", "long-paragraph"] if i in (1, 3) else ["plain-text"]
                    if i == 0:
                        features.append("crlf")
                    if i == 2:
                        features.append("bom")
                elif fmt == "docx":
                    features = ["headings", "table", "bullet-list"]
                    if i in (0, 2, 4):
                        features.append("merged-cells")
                    if i in (1, 3):
                        features.append("page-break")
                    if i in (2, 4):
                        features.append("long-paragraph")
                elif fmt == "csv":
                    features = ["header", "many-rows", "quoted-multiline", "bom"]
                    if i in (1, 3):
                        features.append("uneven-rows")
                elif fmt == "xlsx":
                    features = ["merged-title", "multi-sheet", "many-rows", "dates"]
                    if i in (0, 2):
                        features.append("merged-data")
                elif fmt == "pptx":
                    features = ["slides", "table", "speaker-notes", "section-divider"]
                    if i in (1, 4):
                        features.append("long-bullet")
                elif fmt == "pdf":
                    features = ["headings", "table", "multipage"]
                    if i in (1, 3):
                        features.append("cross-page")
                elif fmt == "png":
                    features = ["ocr", ["scan-notice", "table-screenshot", "dashboard-chart", "ui-screenshot", "long-scan", "scan-notice"][i]]
                title = f"{topic_zh}{'管理办法' if theme == 'policy' else ('运维手册' if theme == 'tech' else '常见问题')}"
                title_en = f"{topic_en}{' Policy' if theme == 'policy' else (' Operations Guide' if theme == 'tech' else ' FAQ')}"
                if lang == "en":
                    title = title_en
                doc = DocSpec(
                    id=f"{fmt}-{lang}-{i + 1:02d}",
                    fmt=fmt,
                    lang=lang,
                    theme=theme,
                    title=title,
                    topic_zh=topic_zh,
                    topic_en=topic_en,
                    slug=slug,
                    code=code,
                    alias=alias,
                    product=product,
                    subject=subject,
                    facts=facts,
                    table_rows=rows,
                    table_headers=table_headers,
                    gold_row=gold_row,
                    features=features,
                    requires_mineru=requires_mineru,
                    extra={"owner": owner, "place": place, "row_code": row_code, "ledger_value": str(6000 + seq * 137), "index": i, "seq": seq},
                )
                docs.append(doc)
    # 两份特殊 PDF：双栏与扫描件，默认路由到 MinerU，用于标注解析边界。
    for lang in ("zh", "en"):
        base = next(d for d in docs if d.fmt == "pdf" and d.lang == lang and d.extra["index"] == 4)
        base.requires_mineru = True
        base.features.append("multicolumn" if lang == "zh" else "scanned")
    return docs


def sentence_fact(doc: DocSpec, fact: Fact, n: int) -> str:
    if doc.lang == "zh":
        if doc.theme == "policy":
            return (
                f"根据{doc.code}的规定，{doc.topic_zh}事项中{doc.subject}的{fact.attr_zh}为{fact.value}。"
                f"该事项责任岗位为{doc.extra['owner']}，相关记录在{doc.extra['place']}留存。"
            )
        if doc.theme == "tech":
            return (
                f"在{doc.product}的{doc.code}配置中，{doc.topic_zh}相关{doc.subject}的{fact.attr_zh}"
                f"默认配置为{fact.value}；如需调整，应通过配置中心提交变更单。"
            )
        return (
            f"关于{doc.topic_zh}（{doc.code}），问：{doc.subject}的{fact.attr_zh}如何计算？\n"
            f"答：{fact.attr_zh}为{fact.value}，以账单页或控制台展示为准。"
        )
    if doc.theme == "policy":
        return (
            f"Under {doc.code}, the {fact.attr_en} for the {doc.subject} in {doc.topic_en} is {fact.value}. "
            f"The accountable role is {doc.extra['owner']}, and records are kept at {doc.extra['place']}."
        )
    if doc.theme == "tech":
        return (
            f"In the {doc.code} configuration of {doc.product}, the {fact.attr_en} for the {doc.subject} "
            f"related to {doc.topic_en} defaults to {fact.value}; any change must go through the configuration center."
        )
    return (
        f"About {doc.topic_en} ({doc.code}), Q: How is the {fact.attr_en} for the {doc.subject} calculated?\n"
        f"A: The {fact.attr_en} is {fact.value}, as shown on the billing page or console."
    )


def section_payload(doc: DocSpec) -> list[dict[str, Any]]:
    """返回五节的标题与段落；事实固定在 FACT_SECTION，表格在 TABLE_SECTION。"""
    h = HEADINGS[doc.theme][doc.lang]
    fs = FACT_SECTION[doc.theme]
    long_doc = ("long-section" in doc.features) or ("long-paragraph" in doc.features) or ("long-bullet" in doc.features) or ("long-scan" in doc.features) or ("cross-page" in doc.features)
    sections: list[dict[str, Any]] = []
    for index, heading in enumerate(h):
        paras: list[str] = []
        if doc.lang == "zh":
            if index == 0:
                if doc.theme == "policy":
                    paras = [f"为规范{doc.topic_zh}相关工作，明确职责边界与执行标准，{COMPANY['zh']}结合业务实际制定本文件。", "本文件适用于总部、各分支机构、驻场团队及远程办公人员；外部合作方参照执行，合同另有约定的从其约定。"]
                elif doc.theme == "tech":
                    paras = [f"本文档说明{doc.topic_zh}的适用范围、依赖、配置步骤与验收标准，供研发、测试和运维人员使用。", "适用于测试、预发布和生产环境。生产环境的任何变更都必须走发布流程，并保留变更单与回滚方案。"]
                else:
                    paras = [f"本页汇总{doc.topic_zh}相关的常见问题，帮助客户快速判断适用范围与操作方式。", "以下说明适用于澄澜知识云的标准版与企业版；私有化部署的差异以合同和交付文档为准。"]
            elif index == fs:
                paras = [sentence_fact(doc, doc.facts[0], 1), sentence_fact(doc, doc.facts[1], 2), "执行过程中应保留必要的审批记录和凭证，不得以口头同意替代书面记录。", "遇到本文件未覆盖的情形，应先向归口部门书面请示，再按批复执行。"]
            elif index == 1:
                if doc.theme == "policy":
                    paras = ["平台与信息安全部负责解释、修订和监督检查。", "各部门负责人负责本部门执行、复核与异常上报。", "财务与行政团队负责凭证、台账和归档的完整性。"]
                elif doc.theme == "tech":
                    paras = ["服务负责人对配置正确性和容量水位负责。", "值班工程师负责变更窗口内的监控与告警处置。", "安全团队负责密钥、证书和访问权限的复核。"]
                else:
                    paras = ["问：配置变更后多久生效？", "答：通常在保存后一分钟内生效，已建立的会话不受影响。"]
            elif index == TABLE_SECTION[doc.theme]:
                paras = ["归口部门每季度抽查一次执行情况，对发现的问题登记整改；涉及金额、权限或客户资料的例外，必须由分管负责人批准。"]
            else:
                if doc.theme == "policy":
                    paras = ["本文件由平台与信息安全部负责解释；此前发布的相关规定与本文件不一致的，以本文件为准。"]
                elif doc.theme == "tech":
                    paras = ["如遇本文档未覆盖的异常，请联系平台值班群；紧急情况可先恢复服务，再补办变更手续。"]
                else:
                    paras = ["本页内容随产品迭代更新，不构成单独的服务承诺；服务等级以正式协议为准。"]
        else:
            if index == 0:
                if doc.theme == "policy":
                    paras = [f"This document sets out how {doc.topic_en} is governed at {COMPANY['en']}, including responsibilities and execution standards.", "It applies to headquarters, branch offices, on-site teams, and remote staff. External partners follow it unless their contract states otherwise."]
                elif doc.theme == "tech":
                    paras = [f"This guide describes the scope, dependencies, configuration steps, and acceptance criteria for {doc.topic_en}. It is intended for engineers, testers, and operators.", "It covers test, staging, and production. Every production change must follow the release process and keep a change record and rollback plan."]
                else:
                    paras = [f"This page collects common questions about {doc.topic_en} so customers can quickly judge scope and operation.", "It applies to the Standard and Enterprise plans of Lantu Knowledge Cloud; private deployments follow the contract and delivery documents."]
            elif index == fs:
                paras = [sentence_fact(doc, doc.facts[0], 1), sentence_fact(doc, doc.facts[1], 2), "Approval records and evidence must be retained; verbal approval does not replace a written record.", "If a case is not covered here, ask the owning team in writing before acting."]
            elif index == 1:
                if doc.theme == "policy":
                    paras = ["The platform and information security team owns interpretation, revision, and supervision.", "Each department head owns execution, review, and escalation within their team.", "Finance and administration own the completeness of evidence, ledgers, and archives."]
                elif doc.theme == "tech":
                    paras = ["The service owner is accountable for configuration correctness and capacity headroom.", "The on-call engineer handles monitoring and alerts during the change window.", "The security team reviews keys, certificates, and access permissions."]
                else:
                    paras = ["Q: How long does a configuration change take to apply?", "A: It usually applies within one minute after saving, and existing sessions are unaffected."]
            elif index == TABLE_SECTION[doc.theme]:
                paras = ["The owning team samples execution quarterly, logs findings for correction, and requires a director's approval for exceptions involving money, authority, or customer data."]
            else:
                if doc.theme == "policy":
                    paras = ["The platform and information security team interprets this document. Where an earlier rule conflicts, this document prevails."]
                elif doc.theme == "tech":
                    paras = ["For exceptions not covered here, contact the platform on-call group. In an emergency, restore service first and complete the change record afterwards."]
                else:
                    paras = ["This page is updated with the product and is not a separate service commitment. Service levels follow the formal agreement."]
        if long_doc and index in (fs, TABLE_SECTION[doc.theme]):
            filler = FILLERS[doc.lang]
            for n in range(10 + doc.extra["seq"] % 6):
                paras.append(filler[n % len(filler)])
        sections.append({"heading": heading, "paras": paras})
    return sections


def md_table(headers: list[str], rows: list[list[str]]) -> str:
    def esc(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", "<br>")
    lines = ["| " + " | ".join(esc(h) for h in headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(esc(c) for c in row) + " |")
    return "\n".join(lines)


def write_md(doc: DocSpec, path: pathlib.Path) -> None:
    sections = section_payload(doc)
    lines: list[str] = []
    if "front-matter" in doc.features:
        lines += ["---", f"title: {doc.title}", f"owner: {doc.extra['owner']}", f"code: {doc.code}", "---", ""]
    else:
        lines += [f"# {doc.title}", "", f"> 文档编号：{doc.code}　责任岗位：{doc.extra['owner']}" if doc.lang == "zh" else f"> Document code: {doc.code}  Owner: {doc.extra['owner']}", ""]
    for index, section in enumerate(sections):
        level = 2
        if "skipped-level" in doc.features and index == 2:
            level = 4
        lines += ["#" * level + " " + section["heading"], ""]
        for para in section["paras"]:
            lines += [para, ""]
        if index == FACT_SECTION[doc.theme] and "code-fence" in doc.features:
            lines += ["```text", "## 这不是标题（代码块内） / This is not a heading", "| 假表头 | 值 |", "| --- | --- |", f"| {doc.code} | 999 |", "```", ""]
        if index == TABLE_SECTION[doc.theme]:
            if "long-list" in doc.features:
                lines += ["- 检查项：审批记录完整" if doc.lang == "zh" else "- Check: approval record complete", "- 检查项：凭证与台账一致" if doc.lang == "zh" else "- Check: evidence matches ledger"]
                for n in range(12):
                    lines.append(f"- {'检查项' if doc.lang == 'zh' else 'Check'} {n + 1}: {doc.alias}-{n + 1:02d}")
                lines.append("")
            lines += [md_table(doc.table_headers, doc.table_rows), ""]
            if "duplicate-heading" in doc.features:
                lines += ["### 附录" if doc.lang == "zh" else "### Appendix", "", "本附录列出常用表单编号，与正文具有同等效力。" if doc.lang == "zh" else "This appendix lists common form codes and has the same effect as the body.", ""]
                lines += ["### 附录" if doc.lang == "zh" else "### Appendix", "", "本附录为补充说明，若与前文冲突以前文为准。" if doc.lang == "zh" else "This appendix is supplementary; the body prevails if they conflict.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def write_txt(doc: DocSpec, path: pathlib.Path) -> None:
    sections = section_payload(doc)
    lines = [doc.title, f"文档编号：{doc.code}" if doc.lang == "zh" else f"Document code: {doc.code}", ""]
    for index, section in enumerate(sections):
        lines += [section["heading"], ""]
        paras = section["paras"]
        if "long-paragraph" in doc.features and index == FACT_SECTION[doc.theme]:
            paras = paras + [" ".join(FILLERS[doc.lang][:10])]
        for para in paras:
            lines += [para, ""]
        if index == TABLE_SECTION[doc.theme]:
            lines += ["\t".join(doc.table_headers)]
            for row in doc.table_rows:
                lines.append("\t".join(row))
            lines.append("")
    text = "\n".join(lines)
    if "crlf" in doc.features:
        path.write_bytes(text.replace("\n", "\r\n").encode("utf-8"))
    elif "bom" in doc.features:
        path.write_text(text, encoding="utf-8-sig")
    else:
        path.write_text(text, encoding="utf-8")


def write_docx(doc: DocSpec, path: pathlib.Path) -> None:
    document = Document()
    document.add_heading(doc.title, 0)
    p = document.add_paragraph(f"文档编号：{doc.code}　责任岗位：{doc.extra['owner']}" if doc.lang == "zh" else f"Document code: {doc.code}  Owner: {doc.extra['owner']}")
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    sections = section_payload(doc)
    for index, section in enumerate(sections):
        if "page-break" in doc.features and index == 2:
            document.add_page_break()
        document.add_heading(section["heading"], level=1 if index == 0 else 2)
        for para in section["paras"]:
            if para.startswith("问：" if doc.lang == "zh" else "Q:"):
                document.add_paragraph(para, style="List Bullet")
            else:
                document.add_paragraph(para)
        if index == TABLE_SECTION[doc.theme]:
            table = document.add_table(rows=1, cols=len(doc.table_headers))
            table.style = "Table Grid"
            for cell, header in zip(table.rows[0].cells, doc.table_headers):
                cell.text = header
                cell.paragraphs[0].runs[0].bold = True
            for row_index, row in enumerate(doc.table_rows):
                cells = table.add_row().cells
                for cell, value in zip(cells, row):
                    cell.text = value
                if "merged-cells" in doc.features and row_index == 0:
                    cells[3].merge(cells[4])
            if "merged-cells" in doc.features:
                table.rows[0].cells[0].merge(table.rows[0].cells[1])
    document.save(str(path))


def ledger_rows(doc: DocSpec, count: int = 80) -> list[list[str]]:
    rows: list[list[str]] = []
    gold_index = 16
    for n in range(count):
        if n == gold_index:
            rows.append([
                doc.extra["row_code"],
                "计算设备" if doc.lang == "zh" else "Compute device",
                f"{doc.alias}{'节点' if doc.lang == 'zh' else ' node'}",
                doc.extra["owner"],
                doc.extra["place"],
                str(2 + n % 7),
                str(6000 + doc.extra["seq"] * 137),
                "在用" if doc.lang == "zh" else "In use",
                f"专项编号 {doc.code}" if doc.lang == "zh" else f"Program code {doc.code}",
            ])
        else:
            rows.append([
                f"{doc.code}-R{n + 1:03d}",
                ["计算设备", "网络设备", "办公家具", "软件许可"][n % 4] if doc.lang == "zh" else ["Compute device", "Network device", "Furniture", "Software license"][n % 4],
                f"{doc.alias}{'辅助' if doc.lang == 'zh' else ' aux'} {n + 1}",
                OWNERS[doc.lang][(doc.extra["seq"] + n) % len(OWNERS[doc.lang])],
                f"{PLACES[doc.lang][n % len(PLACES[doc.lang])]}",
                str(1 + n % 5),
                str(1000 + n * 37),
                "在用" if n % 5 else "闲置",
                "常规记录" if doc.lang == "zh" else "Routine record",
            ])
    return rows


def write_csv(doc: DocSpec, path: pathlib.Path) -> None:
    headers = ["资产编号", "资产类别", "资产名称", "责任人", "存放位置", "数量", "原值(元)", "状态", "备注"] if doc.lang == "zh" else ["Asset code", "Category", "Name", "Custodian", "Location", "Quantity", "Original value (CNY)", "Status", "Note"]
    rows = ledger_rows(doc)
    if "quoted-multiline" in doc.features:
        rows[30][8] = "第一行\n第二行，含逗号, 和引号\"号" if doc.lang == "zh" else "first line\nsecond line, with comma, and \"quotes\""
        rows[55][8] = "含制表符\t的备注" if doc.lang == "zh" else "note with a\ttab"
    if "uneven-rows" in doc.features:
        rows[40] = rows[40][:5]
        rows[41] = rows[41][:5]
        rows[42] = rows[42] + ["额外列1", "额外列2"]
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(headers)
        writer.writerows(rows)


def write_xlsx(doc: DocSpec, path: pathlib.Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "资产台账" if doc.lang == "zh" else "Asset Ledger"
    headers = ["资产编号", "资产类别", "资产名称", "责任人", "存放位置", "数量", "原值(元)", "状态", "购置日期", "备注"] if doc.lang == "zh" else ["Asset code", "Category", "Name", "Custodian", "Location", "Quantity", "Original value (CNY)", "Status", "Purchase date", "Note"]
    last_col = "J"
    ws.merge_cells(f"A1:{last_col}1")
    ws["A1"] = f"{doc.title}——资产台账" if doc.lang == "zh" else f"{doc.title} — Asset Ledger"
    ws["A1"].font = Font(bold=True, size=14)
    ws["A1"].alignment = Alignment(horizontal="center")
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=2, column=col, value=header)
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="DDEBF7")
        cell.alignment = Alignment(horizontal="center")
    rows = ledger_rows(doc, 90)
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for r, row in enumerate(rows, 3):
        for c, value in enumerate(row[:9], 1):
            ws.cell(row=r, column=c, value=value).border = border
        date_cell = ws.cell(row=r, column=9, value=dt.date(2025 + r % 2, 1 + r % 12, 1 + r % 27))
        date_cell.number_format = "yyyy-mm-dd"
        note = ws.cell(row=r, column=10, value=row[8] if len(row) > 8 else "")
        note.border = border
        if "merged-data" in doc.features and r in (10, 25):
            ws.merge_cells(start_row=r, start_column=9, end_row=r, end_column=10)
    ws.freeze_panes = "A3"
    for col, width in zip("ABCDEFGHIJ", [14, 14, 22, 12, 18, 8, 14, 10, 14, 26]):
        ws.column_dimensions[col].width = width

    summary = wb.create_sheet("汇总" if doc.lang == "zh" else "Summary")
    summary.merge_cells("A1:D1")
    summary["A1"] = "资产分类汇总" if doc.lang == "zh" else "Asset Category Summary"
    summary["A1"].font = Font(bold=True, size=13)
    summary.append(["类别", "数量", "原值合计(元)", "备注"] if doc.lang == "zh" else ["Category", "Count", "Total value (CNY)", "Note"])
    for n, category in enumerate(["计算设备", "网络设备", "办公家具", "软件许可"] if doc.lang == "zh" else ["Compute device", "Network device", "Furniture", "Software license"]):
        summary.append([category, 10 + n * 3, 100000 + n * 37000, f"包含 {doc.alias} 相关资产" if doc.lang == "zh" else f"Includes assets related to {doc.alias}"])

    notes = wb.create_sheet("说明" if doc.lang == "zh" else "Notes")
    notes["A1"] = "字段说明" if doc.lang == "zh" else "Field notes"
    notes["A1"].font = Font(bold=True, size=13)
    notes.append([sentence_fact(doc, doc.facts[0], 1)])
    notes.append([sentence_fact(doc, doc.facts[1], 2)])
    notes.merge_cells("B3:C3")
    notes["B3"] = f"文档编号 {doc.code}" if doc.lang == "zh" else f"Document code {doc.code}"
    wb.save(str(path))


def write_pptx(doc: DocSpec, path: pathlib.Path) -> None:
    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    sections = section_payload(doc)

    def bullets(slide, items: list[str]) -> None:
        frame = slide.placeholders[1].text_frame
        frame.clear()
        for index, item in enumerate(items):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            paragraph.text = item
            paragraph.level = 0

    slide = prs.slides.add_slide(prs.slide_layouts[0])
    slide.shapes.title.text = doc.title
    slide.placeholders[1].text = f"{doc.code} · {doc.extra['owner']}"
    slide.notes_slide.notes_text_frame.text = "开场说明：仅使用虚构示例。" if doc.lang == "zh" else "Opening note: fictional examples only."

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "议程" if doc.lang == "zh" else "Agenda"
    bullets(slide, [section["heading"] for section in sections])

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = sections[0]["heading"]
    slide.notes_slide.notes_text_frame.text = sections[0]["paras"][0]

    for index in (1, 2, 3, 4):
        slide = prs.slides.add_slide(prs.slide_layouts[1])
        slide.shapes.title.text = sections[index]["heading"]
        items = sections[index]["paras"][:]
        if "long-bullet" in doc.features and index == 2:
            items.append(" ".join(FILLERS[doc.lang]))
        bullets(slide, items[:6])
        slide.notes_slide.notes_text_frame.text = sections[index]["paras"][0]

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "附录：标准表" if doc.lang == "zh" else "Appendix: Standards Table"
    rows = len(doc.table_rows) + 1
    cols = len(doc.table_headers)
    table_shape = slide.shapes.add_table(rows, cols, Inches(0.8), Inches(1.6), Inches(11.7), Inches(0.5 * rows))
    table = table_shape.table
    for c, header in enumerate(doc.table_headers):
        table.cell(0, c).text = header
    for r, row in enumerate(doc.table_rows, 1):
        for c, value in enumerate(row):
            table.cell(r, c).text = value
    slide.notes_slide.notes_text_frame.text = sentence_fact(doc, doc.facts[0], 1)

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "风险与应对" if doc.lang == "zh" else "Risks and Mitigations"
    bullets(slide, [
        "配置漂移：变更前后核对配置中心快照。" if doc.lang == "zh" else "Configuration drift: compare configuration snapshots before and after.",
        "容量不足：提前两周评估水位并提交扩容。" if doc.lang == "zh" else "Capacity shortfall: assess headroom two weeks ahead and request scaling.",
        "证据缺失：按季度抽查审批与凭证。" if doc.lang == "zh" else "Missing evidence: sample approvals and records quarterly.",
    ])

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "下一步" if doc.lang == "zh" else "Next Steps"
    bullets(slide, ["确认责任人并登记台账。" if doc.lang == "zh" else "Confirm owners and update the ledger.", "在下一次例会上复盘执行情况。" if doc.lang == "zh" else "Review execution at the next meeting."])

    slide = prs.slides.add_slide(prs.slide_layouts[5])
    slide.shapes.title.text = "问答" if doc.lang == "zh" else "Q&A"

    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "备注" if doc.lang == "zh" else "Notes"
    bullets(slide, [sentence_fact(doc, doc.facts[0], 1), sentence_fact(doc, doc.facts[1], 2)])
    prs.save(str(path))


def pdf_wrap(pdf: FPDF, text: str, width: float) -> str:
    """按实际字宽预折行，避开 fpdf2 对无空格中文的 CHAR 折行路径。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        current = ""
        for char in raw:
            if pdf.get_string_width(current + char) <= width:
                current += char
            else:
                lines.append(current)
                current = char
        lines.append(current)
    return "\n".join(lines)


def write_pdf(doc: DocSpec, path: pathlib.Path) -> None:
    pdf = FPDF(format="A4")
    # TTC 在 fpdf2 中会让 pypdf 每字之间多出 NUL；改用 TTF + 拉丁字体回退。
    pdf.add_font("cjk", fname=str(first_existing(PDF_CJK_CANDIDATES)))
    pdf.add_font("latin", fname=str(first_existing(PDF_LATIN_CANDIDATES)))
    pdf.set_fallback_fonts(["latin"])
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()
    width = pdf.w - pdf.l_margin - pdf.r_margin
    pdf.set_font("cjk", size=18)
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 10, pdf_wrap(pdf, doc.title, width), align="C")
    pdf.set_font("cjk", size=9)
    pdf.set_text_color(90, 90, 90)
    subtitle = f"文档编号：{doc.code}　责任岗位：{doc.extra['owner']}" if doc.lang == "zh" else f"Document code: {doc.code}  Owner: {doc.extra['owner']}"
    pdf.set_x(pdf.l_margin)
    pdf.multi_cell(0, 6, pdf_wrap(pdf, subtitle, width), align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)
    sections = section_payload(doc)
    for index, section in enumerate(sections):
        pdf.set_font("cjk", size=13)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 8, pdf_wrap(pdf, section["heading"], width))
        pdf.set_font("cjk", size=10.5)
        for para in section["paras"]:
            pdf.set_x(pdf.l_margin)
            pdf.multi_cell(0, 6.2, pdf_wrap(pdf, para, width))
            pdf.ln(1.2)
        if index == TABLE_SECTION[doc.theme]:
            pdf.set_font("cjk", size=8.5)
            widths = [26, 38, 46, 28, 44]
            pdf.set_x(pdf.l_margin)
            for header, width in zip(doc.table_headers, widths):
                pdf.cell(width, 7, header, border=1, align="C")
            pdf.ln()
            for row in doc.table_rows:
                pdf.set_x(pdf.l_margin)
                for value, width in zip(row, widths):
                    text = value if len(value) <= 20 else value[:19] + "…"
                    pdf.cell(width, 7, text, border=1)
                pdf.ln()
            pdf.ln(3)
    if "multicolumn" in doc.features:
        pdf.add_page()
        pdf.set_font("cjk", size=12)
        left = [f"{doc.subject}", sentence_fact(doc, doc.facts[0], 1), "左栏仅适用于东区入口。"]
        right = [f"{doc.alias}备用通道", sentence_fact(doc, doc.facts[1], 2), "右栏仅适用于西区出口。"]
        for column, block in enumerate((left, right)):
            pdf.set_xy(12 + column * 98, 20)
            pdf.multi_cell(88, 7, pdf_wrap(pdf, "\n".join(block), 88))
    if "scanned" in doc.features:
        pdf.add_page()
        image_path = pathlib.Path(tempfile.gettempdir()) / f"{doc.id}-scan.png"
        draw_text_page(image_path, doc, [doc.title, sentence_fact(doc, doc.facts[0], 1), sentence_fact(doc, doc.facts[1], 2)])
        pdf.image(str(image_path), x=10, y=18, w=190)
    pdf.output(str(path))


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    """优先按空格断词，超宽单词再按字符断；中文无空格时退化为按字符。"""
    lines: list[str] = []
    for raw in text.split("\n"):
        if not raw:
            lines.append("")
            continue
        current = ""
        for token in raw.split(" "):
            candidate = token if not current else current + " " + token
            if draw.textlength(candidate, font=font) <= width:
                current = candidate
                continue
            if current:
                lines.append(current)
            if draw.textlength(token, font=font) <= width:
                current = token
                continue
            current = ""
            for char in token:
                candidate = current + char
                if draw.textlength(candidate, font=font) <= width:
                    current = candidate
                else:
                    lines.append(current)
                    current = char
        lines.append(current)
    return lines


def draw_wrapped(draw: ImageDraw.ImageDraw, text: str, x: int, y: int, font: ImageFont.FreeTypeFont, width: int, line_height: int, fill: str = "black") -> int:
    """按像素宽度逐行绘制，返回下一行的 y；保留原文中的换行。"""
    for line in _wrap(draw, text, font, width):
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height
    return y


def draw_text_page(path: pathlib.Path, doc: DocSpec, paragraphs: list[str]) -> None:
    width, height = 1654, 2339
    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path()), 40)
    title_font = ImageFont.truetype(str(font_path()), 58)
    y = 120
    draw.text((120, y), paragraphs[0][:26], font=title_font, fill="black")
    y += 110
    for para in paragraphs[1:]:
        for line in _wrap(draw, para, font, width - 240):
            if y > height - 140:
                break
            draw.text((120, y), line, font=font, fill="black")
            y += 58
        y += 28
    info = PngInfo()
    info.add_text("Comment", f"rag-baseline-v1-r2 {doc.id}")
    image.save(path, pnginfo=info)


def write_png(doc: DocSpec, path: pathlib.Path) -> None:
    kind = next((f for f in doc.features if f in {"scan-notice", "table-screenshot", "dashboard-chart", "ui-screenshot", "long-scan"}), "scan-notice")
    if kind in {"scan-notice", "long-scan"}:
        paragraphs = [doc.title, sentence_fact(doc, doc.facts[0], 1), sentence_fact(doc, doc.facts[1], 2)]
        if kind == "long-scan":
            paragraphs += FILLERS[doc.lang][:6]
        draw_text_page(path, doc, paragraphs)
        return
    width, height = 1800, 1100
    image = Image.new("RGB", (width, height), "#f7f8fa")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype(str(font_path()), 34)
    small = ImageFont.truetype(str(font_path()), 26)
    textfont = ImageFont.truetype(str(font_path()), 30)
    bold = ImageFont.truetype(str(font_path()), 44)
    if kind == "table-screenshot":
        draw.rectangle((0, 0, width, 90), fill="#1f3a5f")
        draw.text((50, 20), f"{doc.title} · 台账" if doc.lang == "zh" else f"{doc.title} · Ledger", font=bold, fill="white")
        x0, y0 = 60, 150
        col_widths = [330, 330, 300, 190, 240, 140]
        headers = doc.table_headers[:6]
        for x, header in zip([x0 + sum(col_widths[:i]) for i in range(len(headers))], headers):
            draw.rectangle((x, y0, x + col_widths[headers.index(header)], y0 + 70), fill="#dbe7f3", outline="#8ba3bd")
            draw.text((x + 10, y0 + 14), header, font=small, fill="black")
        for r, row in enumerate(doc.table_rows):
            y = y0 + 70 * (r + 1)
            for c, value in enumerate(row[:6]):
                x = x0 + sum(col_widths[:c])
                draw.rectangle((x, y, x + col_widths[c], y + 70), fill="white", outline="#8ba3bd")
                draw.text((x + 10, y + 14), str(value)[:24], font=small, fill="black")
    elif kind == "dashboard-chart":
        draw.rectangle((0, 0, width, 100), fill="#14324f")
        draw.text((50, 22), f"{doc.title} · 监控看板" if doc.lang == "zh" else f"{doc.title} · Dashboard", font=bold, fill="white")
        y = draw_wrapped(draw, sentence_fact(doc, doc.facts[0], 1), 160, 140, textfont, width - 320, 40, "#333333")
        draw_wrapped(draw, sentence_fact(doc, doc.facts[1], 2), 160, y + 12, textfont, width - 320, 40, "#333333")
        values = [42, 68, 55, 81, 74, 93]
        labels = [f"{doc.alias}-{i + 1}" for i in range(len(values))]
        for i, (value, label) in enumerate(zip(values, labels)):
            x = 160 + i * 240
            draw.rectangle((x, 970 - value * 5, x + 130, 970), fill="#3c7fb1")
            draw.text((x, 990), label, font=small, fill="black")
            draw.text((x + 20, 970 - value * 5 - 40), str(value), font=small, fill="black")
    else:  # ui-screenshot
        draw.rectangle((0, 0, width, height), fill="#eef1f5")
        draw.rectangle((80, 100, 1720, 1000), fill="white", outline="#c4ccd6", width=3)
        draw.rectangle((80, 100, 1720, 180), fill="#22405f")
        draw.text((110, 120), f"{doc.title} · 设置" if doc.lang == "zh" else f"{doc.title} · Settings", font=bold, fill="white")
        draw.text((130, 240), f"服务：{doc.subject}" if doc.lang == "zh" else f"Service: {doc.subject}", font=font, fill="black")
        draw.text((130, 310), f"编号：{doc.code}", font=font, fill="black")
        y = draw_wrapped(draw, sentence_fact(doc, doc.facts[0], 1), 130, 400, textfont, 1500, 40, "black")
        y = draw_wrapped(draw, sentence_fact(doc, doc.facts[1], 2), 130, y + 16, textfont, 1500, 40, "black")
        draw.rectangle((130, max(y + 40, 760), 430, max(y + 40, 760) + 80), fill="#2f6fb0")
        draw.text((180, max(y + 40, 760) + 20), "保存" if doc.lang == "zh" else "Save", font=font, fill="white")
    info = PngInfo()
    info.add_text("Comment", f"rag-baseline-v1-r2 {doc.id}")
    image.save(path, pnginfo=info)


WRITERS = {
    "md": write_md,
    "txt": write_txt,
    "docx": write_docx,
    "csv": write_csv,
    "xlsx": write_xlsx,
    "pptx": write_pptx,
    "pdf": write_pdf,
    "png": write_png,
}


def document_features(doc: DocSpec) -> list[str]:
    return list(dict.fromkeys(doc.features))


def make_queries(doc: DocSpec) -> list[dict[str, Any]]:
    fact1 = doc.facts[0]
    queries: list[dict[str, Any]] = []
    data_doc = doc.fmt in ("csv", "xlsx")
    if doc.lang == "zh":
        lexical = f"资产台账中编号为 {doc.extra['row_code']} 的资产，原值是多少元？" if data_doc else f"{doc.code} 中规定的{fact1.attr_zh}是多少？"
        paraphrase = f"{doc.alias}在{fact1.attr_zh}方面执行什么标准？"
        table_query = f"资产台账中编号为 {doc.extra['row_code']} 的记录，责任人是谁、存放在哪里？"
    else:
        lexical = f"In the asset ledger, what is the original value of asset {doc.extra['row_code']}?" if data_doc else f"What does {doc.code} specify as the {fact1.attr_en}?"
        paraphrase = f"What standard does {doc.alias} follow for its {fact1.attr_en}?"
        table_query = f"In the asset ledger, who is responsible for record {doc.extra['row_code']}, and where is it stored?"
    lexical_needle = doc.extra["ledger_value"] if data_doc else fact1.value
    lexical_section = "台账" if data_doc else HEADINGS[doc.theme][doc.lang][FACT_SECTION[doc.theme]]
    queries.append({
        "id": f"{doc.id}-lex",
        "query": lexical,
        "lang": doc.lang,
        "doc_lang": doc.lang,
        "format": doc.fmt,
        "theme": doc.theme,
        "type": "lexical",
        "difficulty": "easy",
        "answerable": True,
        "doc": doc.id,
        "gold": {"needles": [lexical_needle], "section": lexical_section, "value": lexical_needle, "same_chunk": True},
    })
    if doc.fmt in ("csv", "xlsx"):
        second = {
            "id": f"{doc.id}-row",
            "query": table_query,
            "lang": doc.lang,
            "doc_lang": doc.lang,
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "table_row",
            "difficulty": "medium",
            "answerable": True,
            "doc": doc.id,
            "gold": {"needles": [doc.extra["row_code"], doc.extra["owner"], doc.extra["place"]], "section": "台账", "value": doc.extra["row_code"], "same_chunk": True},
        }
    else:
        second = {
            "id": f"{doc.id}-para",
            "query": paraphrase,
            "lang": doc.lang,
            "doc_lang": doc.lang,
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "paraphrase",
            "difficulty": "medium",
            "answerable": True,
            "doc": doc.id,
            "gold": {"needles": [fact1.value], "section": HEADINGS[doc.theme][doc.lang][FACT_SECTION[doc.theme]], "value": fact1.value, "same_chunk": True},
        }
    queries.append(second)
    return queries


def add_advanced_queries(docs: list[DocSpec], queries: list[dict[str, Any]]) -> None:
    # 跨节双事实：检验 top-k 能否覆盖同一文档中的两个不同片段。
    candidates = [d for d in docs if d.fmt in ("md", "docx", "pdf", "pptx") and not d.requires_mineru]
    for doc in candidates[:8]:
        fact1, fact2 = doc.facts
        if doc.lang == "zh":
            text = f"{doc.code} 中的{fact1.attr_zh}和{fact2.attr_zh}分别是多少？"
        else:
            text = f"What are the {fact1.attr_en} and the {fact2.attr_en} specified in {doc.code}?"
        queries.append({
            "id": f"{doc.id}-cross",
            "query": text,
            "lang": doc.lang,
            "doc_lang": doc.lang,
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "cross_section",
            "difficulty": "hard",
            "answerable": True,
            "doc": doc.id,
            "gold": {"needles": [fact1.value, fact2.value], "section": HEADINGS[doc.theme][doc.lang][FACT_SECTION[doc.theme]], "value": f"{fact1.value} / {fact2.value}", "same_chunk": False},
        })
    # 跨语言：用另一种语言提问，文档内容仍为原语言；检验多语言向量召回。
    cross_docs = [d for d in docs if d.fmt in ("md", "docx", "pdf") and not d.requires_mineru]
    zh_docs = [d for d in cross_docs if d.lang == "zh"][:4]
    en_docs = [d for d in cross_docs if d.lang == "en"][:4]
    for doc in zh_docs:
        fact = doc.facts[0]
        queries.append({
            "id": f"{doc.id}-xlang",
            "query": f"What does {doc.code} specify as the {fact.attr_en}?",
            "lang": "en",
            "doc_lang": "zh",
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "cross_language",
            "difficulty": "hard",
            "answerable": True,
            "doc": doc.id,
            "gold": {"needles": [fact.value], "section": HEADINGS[doc.theme]["zh"][FACT_SECTION[doc.theme]], "value": fact.value, "same_chunk": True},
        })
    for doc in en_docs:
        fact = doc.facts[0]
        queries.append({
            "id": f"{doc.id}-xlang",
            "query": f"{doc.code} 中规定的{fact.attr_zh}是多少？",
            "lang": "zh",
            "doc_lang": "en",
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "cross_language",
            "difficulty": "hard",
            "answerable": True,
            "doc": doc.id,
            "gold": {"needles": [fact.value], "section": HEADINGS[doc.theme]["en"][FACT_SECTION[doc.theme]], "value": fact.value, "same_chunk": True},
        })
    # 同主题无答案：文档存在且高度相关，但事实不在文档中。
    negatives = [d for d in docs if d.fmt in ("md", "docx", "pdf", "txt") and not d.requires_mineru][:10]
    for n, doc in enumerate(negatives):
        if doc.lang == "zh":
            text = f"{doc.code} 中规定的采购单价是多少？"
        else:
            text = f"What is the purchase price specified in {doc.code}?"
        queries.append({
            "id": f"neg-same-doc-{n + 1:02d}",
            "query": text,
            "lang": doc.lang,
            "doc_lang": doc.lang,
            "format": doc.fmt,
            "theme": doc.theme,
            "type": "no_answer",
            "difficulty": "hard",
            "answerable": False,
            "doc": doc.id,
            "gold": {"needles": [], "section": None, "value": None, "same_chunk": False},
        })
    # 完全不存在实体：应返回空或低分候选。
    for n in range(10):
        lang = "zh" if n % 2 == 0 else "en"
        text = f"青砾-9{n + 1:02d} 的默认超时是多少？" if lang == "zh" else f"What is the default timeout for ember-9{n + 1:02d}?"
        queries.append({
            "id": f"neg-missing-{n + 1:02d}",
            "query": text,
            "lang": lang,
            "doc_lang": lang,
            "format": None,
            "theme": None,
            "type": "no_answer",
            "difficulty": "easy",
            "answerable": False,
            "doc": None,
            "gold": {"needles": [], "section": None, "value": None, "same_chunk": False},
        })


def main() -> None:
    CORPUS.mkdir(parents=True, exist_ok=True)
    docs = build_docs()
    manifest_docs: list[dict[str, Any]] = []
    queries: list[dict[str, Any]] = []
    for doc in docs:
        directory = CORPUS / doc.fmt
        directory.mkdir(parents=True, exist_ok=True)
        filename = f"{doc.lang}-{doc.extra['index'] + 1:02d}-{doc.slug}.{doc.fmt}"
        path = directory / filename
        print("GEN", doc.id, flush=True)
        WRITERS[doc.fmt](doc, path)
        if doc.fmt in ("csv", "xlsx"):
            check_terms = LEDGER_HEADERS[doc.lang]
            gold_cells = ledger_rows(doc)[16]
        elif doc.fmt == "png":
            check_terms = [doc.title, doc.code, doc.alias]
            gold_cells = None
        else:
            check_terms = HEADINGS[doc.theme][doc.lang] + doc.table_headers
            gold_cells = None
        manifest_docs.append({
            "id": doc.id,
            "file": str(path.relative_to(ROOT)).replace("\\", "/"),
            "format": doc.fmt,
            "lang": doc.lang,
            "theme": doc.theme,
            "title": doc.title,
            "code": doc.code,
            "alias": doc.alias,
            "features": document_features(doc),
            "requires_mineru": doc.requires_mineru,
            "check_terms": check_terms,
            "facts": [
                {"attr": fact.attr_zh if doc.lang == "zh" else fact.attr_en, "value": fact.value, "section": HEADINGS[doc.theme][doc.lang][FACT_SECTION[doc.theme]]}
                for fact in doc.facts
            ],
            "gold_row": {
                "code": doc.extra["row_code"],
                "cells": gold_cells,
                "owner": doc.extra["owner"],
                "place": doc.extra["place"],
            } if gold_cells else None,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "bytes": path.stat().st_size,
        })
        queries.extend(make_queries(doc))
    add_advanced_queries(docs, queries)
    manifest = {
        "version": 1,
        "frozen_at": "2026-11-11",
        "company": "澄川科技 / Chengchuan Labs（虚构）",
        "rules": "全部为自撰虚构内容，不含真实单位资料；冻结后不按结果修改题目。",
        "counts": {
            "documents": len(manifest_docs),
            "queries": len(queries),
            "positive": sum(1 for q in queries if q["answerable"]),
            "negative": sum(1 for q in queries if not q["answerable"]),
            "by_format": {fmt: sum(1 for d in manifest_docs if d["format"] == fmt) for fmt in FORMATS},
            "by_lang": {lang: sum(1 for d in manifest_docs if d["lang"] == lang) for lang in ("zh", "en")},
            "requires_mineru": sum(1 for d in manifest_docs if d["requires_mineru"]),
        },
        "documents": manifest_docs,
        "queries": queries,
    }
    (ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest["counts"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
