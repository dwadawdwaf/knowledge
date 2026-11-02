# 运行手册

根目录命令均在项目根目录执行；接口语义见[设计](design.md)，未完成项见[issues](../issues.md)。

## 1. 安装与配置

解释器版本以.python-version为准，依赖用backend/requirements-dev.lock，不只装pyproject的浮动范围。
首次安装需要允许下载，已有环境不盲目删除或重装：

```powershell
py -3.12 -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements-dev.lock
```

py -3.12不保证补丁版本。脚本从源码运行，不要求editable安装。
配置放backend/.env或环境变量，不提交密钥：

| 配置 | 用途 |
| --- | --- |
| KS_EMBEDDING_BASE_URL / KS_EMBEDDING_MODEL / KS_EMBEDDING_API_KEY / KS_EMBEDDING_DIM | 向量服务与1024维配置 |
| KS_GENERATOR_BASE_URL / KS_GENERATOR_MODEL / KS_GENERATOR_API_KEY | OpenAI兼容聊天服务 |
| KS_DATABASE_URL | 显式数据库；根启动器未设置时自动用本机内置库 |
| KS_WORKBENCH_OBJECTS | 根启动器持久原件目录，默认backend/.data/objects |
| KS_STORAGE_ROOT | 手动运行API/Worker的原件目录，不等同根启动器的卷配置 |
| KS_DOCUMENT_PARSER_PROFILE | 非PDF的默认解析策略，只影响新上传/新版本 |
| KS_PDF_PARSER_PROFILE | PDF默认mineru-api4.1-v1，显式选择优先；重建保留原策略 |
| KS_TOKEN_SECRET | 本地开发身份桩签名密钥，正式接入替换验证器 |

模型配置可能向外部服务发送内容，真实资料须另授权。换向量模型必须离线迁移索引、重建和重标阈值，不在现有索引混写。

## 2. 启动与数据保留

```powershell
.\start.cmd
.\start.cmd -Mode Demo
.\start.cmd -CheckOnly
.\start.cmd -NoBrowser -Port 8011
```

| 模式 | 行为 |
| --- | --- |
| 默认Real | 真实模型、持久库及原件卷，退出后资料保留 |
| Demo | 临时隔离库及原件目录、确定性模型桩，退出清理，不代表模型效果 |
| CheckOnly | 检查配置/依赖及模型，不进入问答；可能启动依赖，不是完全无副作用 |

启动器输出阶段、端口、/workbench/与/docs地址；终端可提问，/token显示开发令牌，/quit退出。
令牌属本次启动桩，服务重启后重新连接，不写进报告或提交。
依赖失败停止，不静默切模型桩；-PullMissing才允许拉取缺失向量模型，-NoDeepDoc显式关闭本次DeepDoc。
Real模式自动调用start-mineru.ps1启动或复用已安装的MinerU；-MinerURoot可指定D盘环境目录（默认D:\MinerU），
-NoMinerU仅跳过自动启动，不切换默认或已有文件的解析方案。Demo不启动MinerU；CheckOnly也会准备该依赖。
未安装时明确提示且不自动安装；已安装但模型/接口检查失败则停止启动，不静默回退。

未显式设置数据库时复用/启动本机PostgreSQL，默认55433端口。
数据目录优先已初始化的backend/.data/pgdata，否则本机应用数据目录；KS_LOCAL_DB_DIR可指定ASCII路径。
API/Worker退出不等于后台数据库停止，停止用：

```powershell
.\backend\.venv\Scripts\python.exe backend/scripts/dev_db.py --stop
```

**不要为排障使用dev_db.py --reset，它会删除整个库。** 数据库与原件目录成对备份/迁移，临时库不得共用持久原件卷。

### 手动部署与迁移

确认目标KS_DATABASE_URL/KS_STORAGE_ROOT，先备份并停止Worker、处理旧任务。
在backend目录执行，API和Worker分别在不同终端运行且使用同一配置：

```powershell
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m uvicorn knowledge.entrypoints.api:app --host 127.0.0.1 --port 8010
.\.venv\Scripts\python.exe -m knowledge.entrypoints.worker
```

当前head为0003_chunk_warnings；0002固定任务目标，升级前不能留下未处理旧任务；0003保存块警告。
不对未知默认库运行迁移，不改历史迁移，不原地回填已发布片段。

### 本次数据库回退与备份

2026-10-30已将本机knowledge库由0005事务降至0003，移除快照表和库描述列；
8张核心业务表记录数与原件保持不变。保留最新上传和片段，不等于恢复历史时间点的数据内容。
备份先恢复到独立副本并试降级，验证业务表/列和记录数后才改原库。
备份在忽略目录 `backend/.data/rollback-backups/20261030T021547Z/`：before.dump是完整custom格式备份，
manifest.json记录版本和计数，迁移源码副本用于恢复演练。含业务资料，仅本地保存，禁止提交/外发。
恢复时先用pg_restore到新库验证，再评审切换；不要直接覆盖当前库，也不重复运行一次性降级脚本。

### 建立持久基础评测库（会清空当前业务数据）

仅明确要求替换当前数据时运行，不作为日常启动命令：

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 -u backend/scripts/retrieval_baseline.py --reset-current --confirm-real --serve
```

脚本仅接受`.data/db_url`记录的本机knowledge库及项目`.data/objects`卷；备份在
`backend/.data/evaluation-backups/<UTC时间>/`，包含before.dump、旧objects和校验清单。
清空前自动停止精确匹配该库/卷的API/Worker，原启动终端将断开；发现其他数据库连接则停止，不强制踢掉。
先实际恢复到独立副本校验记录数和版本，成功才清空。原件留在配套备份，不外发。
导入11份自制文件（5种格式），固定24题/8维度，对比4种召回配置并执行24次问答；上限120次检索/问答，
使用现有真实模型，只有自制资料会被提交。入库和报告发布另需向量调用，费用未知，失败不自动重跑。
每6题发布一次进度；前端“检索评测”可刷新查看过程、结果、标注、实际来源并跳转知识库复测或历史问答。
`--serve`使实验后前端继续运行，Ctrl+C停止服务；不加则完成后退出，数据持久保留，下次start.cmd可继续查看。
报告库应与被测资料库分开选择，不能将含标注答案的报告库纳入被测检索范围。

## 3. 同模型重建与人工恢复

1. 保存上传回执document_id/version_id/job_id，queued/running时等待，不重复发请求。
2. failed后先修模型/Worker/解析环境；加密或空原件应更正后上传新版本。
3. 读取最新generation，使用**最新上传回执version_id**，不是仍可能指向旧发布的current_version_id。
4. POST /api/v1/documents/{id}/reindex，UUID与代次换成实际值：

```json
{"version_id":"最新上传版本UUID","expected_generation":3}
```

202后查新job，409刷新详情与已有任务，不盲目重发。重建复用原件/版本和冻结配置，旧发布继续服务直到成功切换。
不支持在线换向量、历史回滚或表格/语义策略升级；原样重传去重不能代替恢复。

## 4. DeepDoc与D盘Docker

需要Linux容器引擎及Compose，初次安装/拉镜像需要授权。项目机器Docker程序用D:\Docker\Desktop，
数据用D:\Docker\wsl，下载与临时文件也在D盘；少量Windows系统配置不等于完全零C盘写入。
install-docker.ps1只处理指定安装路径；用start-docker.ps1启动以指定D盘用户配置，避免原C盘配置改名失败。
不要擅自移动现有WSL盘或改其他发行版，也不要绕过该脚本使用普通快捷方式。

```powershell
.\start-docker.ps1
.\start-deepdoc.ps1 -CheckOnly
.\start-deepdoc.ps1 -LaunchWorkbench
```

仅启动边车可不加LaunchWorkbench，再在同一PowerShell运行start.cmd。
脚本生成共享令牌但不打印；API/Worker配置KS_DOCUMENT_PARSER_PROFILE、KS_DEEPDOC_URL、KS_DEEPDOC_TOKEN，
与边车DEEPDOC_TOKEN一致且至少32字符，换令牌后两端一起重启。KS_DEEPDOC_TIMEOUT_SECONDS是等待上限而非性能目标。

```powershell
Invoke-RestMethod http://127.0.0.1:9381/health
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml logs -f
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml exec deepdoc /opt/ks-sidecar-venv/bin/python -m deepdoc_sidecar.smoke
docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml down
```

Compose命令需同会话令牌；工作台退出不停止边车。health只证明存活，smoke是自制样本，不代表复杂业务验收。
真实启动器未显式配置DeepDoc令牌时，可只读复用本项目ks-deepdoc/deepdoc正在运行容器的令牌，
在内存中传给API与Worker，不打印、不落盘；显式令牌不覆盖，其他容器/地址不读取。更新后重启项目才生效。
保持解析器内部网络禁外连，网关只转发固定目标；缺模型/非法输出失败，不改用旧解析器兜底。
镜像可用RAGFLOW_IMAGE固定同release digest，许可保留在deploy/deepdoc/LICENSE.ragflow，传递依赖另遵守自身许可。

## 5. 验证与评测

### 解析器三选一与MinerU

上传区选择“沿用服务默认 / 基础解析 / DeepDoc / MinerU”，该选择也用于上传新版本。
API在multipart中传parser_profile；选项与冻结语义见设计“显式可选解析器”。PDF默认MinerU，可配KS_PDF_PARSER_PROFILE；其他格式默认可配KS_DOCUMENT_PARSER_PROFILE，
两端须重启到同一版本。重试/普通重建不换策略，同字节去重提示实际已有方案，不把选择当作已切换。
TXT/MD直接读取并告警，不依赖OCR服务；基础模式没有图片OCR，选基础上传图片仍报不支持。
DeepDoc v2支持Office原生路径，须重建并重启已有边车，新客户端不能假定旧镜像已支持v2。
在已设置DEEPDOC_TOKEN的会话执行：`docker compose -p ks-deepdoc -f deploy/deepdoc/compose.yaml up -d --build`；
没有本地镜像或模型缓存时可能联网，先确认下载安排，再执行。
若只有镜像站不可达且已有可信v0.22.0边车镜像，可用deploy/deepdoc/Dockerfile.refresh制作离线代码更新层：
`docker build --pull=false --network=none -f deploy/deepdoc/Dockerfile.refresh -t ks-deepdoc-local:deepdoc-v0.22.0-v2 .`。
旧Docker在BuildKit仍请求镜像站时，可在该会话临时设DOCKER_BUILDKIT=0，完成后恢复；没有旧镜像时不能使用此路径。
随后在有DEEPDOC_TOKEN的会话执行Compose up -d --no-build --pull never；不使用docker cp制作无法复现的运行态补丁。

MinerU固定本机4.1.2独立环境，主环境不安装其重依赖。用户已批准本次在D盘安装与下载basic模型；
后续启动默认MINERU_MODEL_SOURCE=local，缺模型明确失败，不自动联网下载。
准备环境时先将TEMP/TMP、PIP_CACHE_DIR指向D:\MinerU\tmp和D:\MinerU\cache\pip，
用已有Python创建D:\MinerU\venv，再用该环境pip安装mineru==4.1.2；不使用系统pip或主后端环境。
按官方[同步文件API](https://opendatalab.github.io/MinerU/usage/file_parse/)部署，项目根目录提供：

```powershell
.\start-mineru.ps1 -DownloadModels -CheckOnly # 仅显式下载并验证，不启动服务
.\start-mineru.ps1                          # 本地模型检查，启动8000/8003两个回环服务
.\start.cmd                                # 自动准备已安装的MinerU，再启动项目
```

启动脚本将环境/模型/缓存/临时文件/状态/日志放D:\MinerU，服务在后台隐藏窗口运行，打印PID与就绪进度；
日志位于D:\MinerU\logs，退出工作台不自动停止解析服务。复用前校验接口，不停止占用端口的其他进程。
启动脚本不改写KS_MINERU_URL；默认对应8003，若显式配置其他端点，仍以该配置为准。
确认API未被配置为外部Router/SaaS；wrapper本身不加载模型，不代表上游不会转发。
主项目配置KS_MINERU_URL=http://127.0.0.1:8003，可选KS_MINERU_TOKEN供本机认证代理，
KS_MINERU_TIMEOUT_SECONDS为总等待预算（默认330秒），而非性能承诺。代理必须是本机origin；不接外网地址或任意响应URL。
请求固定basic/auto/all，不对长文档只取前10页；超时或异常明确失败，不回退DeepDoc或基础解析。
上游可能在超时/断连后继续工作，本项目不自动重提同次HTTP请求；须按本机服务的保留/清理规则处理其缓存。
4.1 ZIP要求structured_content.json与完整标记，限制压缩/解压预算，不解压任意路径或获取图片。
当前客户端以页/块定位，不承诺字框、Office物理分页或完整表格语义，相关warnings需人工核对。
实际OCR/版面效果验收独立于协议测试，当前已验证范围与遗留问题见根目录issues。
上游[输出契约](https://opendatalab.github.io/MinerU/reference/output_files/)及[许可](https://github.com/opendatalab/MinerU/blob/master/LICENSE.md)须按部署版本核对。

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/check.py
node --test frontend/core.test.mjs
.\backend\.venv\Scripts\python.exe -u backend/scripts/browser_smoke.py
```

后端按预检、lint、类型、独立数据库回归/迁移/HTTP验收执行，不调用真实模型、不修改业务库。
跳过/xfail/排除用例不能算完整通过。非Windows中文PDF测试需KS_TEST_FONT_PATH指定已有字体。
浏览器检查仅在相关前端改动收尾运行，需要已有Node/测试浏览器，不自动安装。
CI定义在.github/workflows/backend.yml，文件存在不等于远端已实跑。

固定12题自制集评测默认离线；真实调用先获得授权，confirm参数不替代授权：

```powershell
.\backend\.venv\Scripts\python.exe -u backend/scripts/evaluate_local.py
.\backend\.venv\Scripts\python.exe -u backend/scripts/evaluate_local.py --real --confirm-real
```

真实模式使用固定自制资料及临时库/API/Worker，48次检索/问答API请求、上限60，不自动重跑。
上传向量化和轮询是额外调用，不据此推断供应商token或费用。报告在backend/.data/evaluation，
只存指纹、配置名、指标与耗时，不存正文/密钥/端点。词面覆盖、结构引用有效均不等于事实正确。

### 持久库第二轮公开长文档评测

仅限已批准的六份官方公开资料和已冻结的48题；不清空旧库，不重新下载语料。
原件、来源清单和标注位于`backend/.data/public-corpus-v1`，不提交；不能替换成私人或甲方资料。

本机16GiB内存的本轮配置：启动MinerU前设置`$env:MINERU_PROCESSING_WINDOW_SIZE='8'`，
再运行`./start-mineru.ps1`；只缩小批处理窗口，不减少页数或关闭识别功能。已运行服务需核对其启动环境，
复用服务不会使新环境变量生效。实验进程设置`$env:KS_PARSE_OUTPUT_BYTES='134217728'`（128MiB），
预检与后台Worker使用同一有界预算，实际值记入预检报告；不修改服务默认16MiB或放开ZIP成员校验。

```powershell
.\backend\.venv\Scripts\python.exe -X utf8 -u backend/scripts/evaluate_public.py --prepare-only
.\backend\.venv\Scripts\python.exe -X utf8 -u backend/scripts/evaluate_public.py --confirm-public --serve --port 8013
```

第一条复用生产解析器做本地预检，不入库或调用模型。第二条保留现有库，新增第二轮资料库，
发布过程与结果并打开工作台；在“检索评测”选择轮次、开发集或留出集，也可查看实际问答与引用。
本轮改用MinerU和现有bge-m3/deepseek-flash配置，不做解析器配对对照或调参；检索/问答最多240次，
入库与报告发布的向量调用另计。`--confirm-public`不替代用户授权。
本次已批准的入库恢复使用`--resume-ingestion --confirm-public --serve --port 8010`；须先停止同库旧工作台/Worker，
保留run-started.json，不能删标记重跑。只允许尚无本轮报告的恢复一次，已有报告时停止并核对请求记录。
`backend/.data/public-round2-mineru`保存预检、启动标记及报告；之前未进入问答的基础解析尝试保留在`backend/.data/public-round2`。
启动标记存在时拒绝再次执行，
中断后先核对已有结果，不删标记碰运气重跑。关闭终端会停止本轮工作台，数据库和原件继续保留。
日常只看结果时用普通启动器，不重新执行评测；报告中词面命中和引用覆盖不等同于答案事实正确率。

## 6. 排障

| 现象 | 先检查 |
| --- | --- |
| 页面无法访问 | 启动器实际地址、安全端口；占用换-Port，不擅自杀其他进程 |
| 401/404 | 本次令牌、资源归属、删除状态；UUID不是凭证 |
| running停滞 | Worker、stage/attempt、租约、模型等待，不手改任务状态 |
| 模型故障 | 端点、认证、余额、名称、1024维及索引模型兼容性，不把故障记成无资料 |
| Ollama绑定失败/10013 | 检查Windows保留端口：`netsh interface ipv4 show excludedportrange protocol=tcp`；不要删系统规则。若11434被保留，将backend/.env的KS_EMBEDDING_BASE_URL改为经检查可用的本机端口（如http://127.0.0.1:21434/v1）。启动器为serve/pull同步设置OLLAMA_HOST，不改系统环境，不更换模型或下载资料；手动启动时也需在该终端设置同一OLLAMA_HOST。 |
| PARSE_FAILED | 格式/加密/资源限制、DeepDoc网络/令牌/模型，不循环重传相同内容 |
| no_evidence | 范围、发布状态、阈值及原文是否支持，不用降低门槛制造答案 |
| 409 | 刷新文档/任务，不重复旧代次 |
| 原文404但分块在 | 数据库与持久原件卷是否配对，是否曾误用临时目录 |

按request_id关联请求，任务再看job_id/build_id、阶段与耗时。日志不加正文或密钥，外发前脱敏。
生产硬配额与身份接入另配置，不把采样资源保护当安全沙箱。
