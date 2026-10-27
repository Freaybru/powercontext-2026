# 回声杂货铺

Dify + PowerContext 互动游戏。玩家寄存一段虚构回忆，完成三次脑洞选择，收集道具与结局；开启新的 Dify 会话后，店长仍能回忆之前的故事。

Dify 编排输入校验、记忆读取、模型调用、结果检查和回合提交。PowerContext 持久化回忆、剧情状态、道具与结局；服务不可用时直接报错，不以其他存储替代。模型负责叙事，程序负责规则，模型失败不推进游戏。

## 文件与依赖

- `dsl/echo_shop.workflow.yml`：唯一需要导入的 Workflow。
- `backend/`：配套服务、游戏规则与网页入口。
- `scripts/`：安装、启动、DSL 生成和验收工具。
- `docs/`：玩法规则、技术设计和完整验证样例。
- `evidence/`：经过脱敏的真实运行结果。
- `requirements.txt`：固定版本运行依赖，包含 PowerContext 的指定源码提交。

运行环境为 Python 3.12、uv、Git；Dify 目标版本为 1.9.1，DSL schema 为 0.4.0。PowerContext 固定在官方提交 `d16fb73a3a887e280699f93b4a5d4c1bf6cf6ed0`。无需知识库、Embedding、Rerank 或自定义插件。

## 安装与启动

在仓库根目录进入项目：

```sh
cd dify
python scripts/bootstrap.py
```

bootstrap 将依赖安装到项目内的 .venv，并生成随机服务密钥到 .env，保留已有配置。运行 PowerContext 与配套服务：

```powershell
# Windows
.\.venv\Scripts\python.exe scripts/run.py
```

```sh
# Linux / macOS
.venv/bin/python scripts/run.py
```

入口为 http://127.0.0.1:18742 。所有可变数据、缓存和日志保存在项目内的点号目录，不需要预置存档。按 Ctrl+C 停止服务。不要把 .env、运行数据库或缓存提交到仓库。

启动器会检查端口占用，等待两个服务健康就绪后才显示入口。需要更换记忆服务端口时，同步修改.env中的POWERCONTEXT_SERVER_HTTP_PORT和POWERCONTEXT_URL；ECHO_PORT控制网页端口，Dify的ECHO_BASE_URL应使用相应地址。

## Dify 配置

有可用 Docker 的环境可执行 `python scripts/prepare_dify.py --start`，准备并启动官方 Dify 1.9.1；默认控制台入口为 http://localhost:18080/install 。首次建立管理员后，导入 `dsl/echo_shop.workflow.yml`。工作流只使用内置 Code 和 HTTP Request 节点，不需要安装模型提供商插件。

填写工作流环境变量：

| 名称 | 内容 |
| --- | --- |
| ECHO_BASE_URL | Dify 可访问的配套后端根地址 |
| ECHO_API_TOKEN | 与配套服务 .env 中同名值一致，使用 Secret 类型 |
| MODEL_URL | OpenAI 兼容的完整 Chat Completions 地址，包含 /chat/completions |
| MODEL_NAME | 模型服务中的实际模型 ID |
| MODEL_API_KEY | 模型凭据，使用 Secret 类型；无鉴权的隔离模型服务按其要求填写占位值 |

模型需接收 model、messages、stream:false、temperature、max_tokens，返回 choices[0].message.content 字符串。 没有现成模型服务时，运行 `python scripts/serve_model.py`，脚本将固定版本的 llama.cpp 和官方 Qwen2.5-1.5B-Instruct 量化权重下载到项目 .runtime/model，并校验SHA256。CPU运行建议预留4GB内存和约2GB磁盘；Linux/Windows x86-64有预置下载支持。按脚本提示把随机生成的模型密钥填入Dify Secret。Docker访问宿主机模型时，按网络需要使用 `--host 0.0.0.0`；服务启用API Key鉴权。既可使用已有的模型服务，也可使用本地部署的开放权重模型；项目不绑定付费供应商。

Docker 同机部署时，配套 .env 设置 ECHO_HOST=0.0.0.0，Dify 的 ECHO_BASE_URL 可以使用 http://host.docker.internal:18742 。Linux Docker 需要 host-gateway 映射，见 deploy/。远端 Dify 应使用它实际可达的后端地址，不能使用别的机器上的 localhost。保留接口鉴权；只按需要开放访问。

在 Dify 中测试“开场”，确认全部节点成功，再发布应用。若使用配套网页的 Dify AI 模式，在配套 .env 配置：

```dotenv
DIFY_API_URL=http://localhost:18080/v1
DIFY_APP_API_KEY=REPLACE_WITH_WORKFLOW_API_KEY
```

这里是配套后端访问 Dify 的方向，与 ECHO_BASE_URL 相反。重启配套服务后选择 Dify AI 模式。Dify Key 和模型 Key 不发送给浏览器。

## 玩法与验证

每次执行输入 player_code 和 action。同一暗号跨 Dify 会话使用同一存档；不同暗号隔离。暗号是试玩存档的取回凭据，请使用随机字母数字，不要使用真实密码。

```text
player_code: reviewer_sample_01
依次输入 action：
寄存：奶奶在蓝铁盒里藏了一颗橘子糖
1
1
1
回访
新周目
```

三次选择1获得“灯火守夜人”，三次选择2获得“宇宙整活王”，三次选择3获得“月光合伙人”。可输入“2：我给乌云开一张月亮味的罚单”增加台词素材。新周目保留寄存回忆和结局，重置本轮道具与分数；全新暗号用于恢复初始测试条件。

详细初始状态、步骤和预期表现见 docs/验证样例.md。真实全链路验收从发布后的 Dify Workflow API 执行，证据只能由实际成功调用生成，不用模拟模型替代。检查故障时先“回访”核对进度：最终响应丢失不等于写入没有发生。

配置发布后的Dify API后执行 `python scripts/verify_workflow.py`，生成脱敏的evidence/workflow.json。正式材料同时包含evidence/checks.json与实际试玩截图：覆盖真实九节点执行、三种结局、玩家隔离、模型失败不提交，以及服务重启后的记忆恢复。模型响应包含真实模型名称与token用量；没有收集机器路径、服务地址、进程信息或凭据。

规则单元测试使用 `python -m pytest tests -q`；安装开发依赖见requirements-dev.txt。`python scripts/check_release.py`检查发布文件是否含机器路径或配置中的真实密钥；`python scripts/package_submission.py`只打包审核过的源文件和证据，输出到被Git忽略的.runtime/release目录。

网页只调用真实 Dify 工作流，没有模拟模型或规则演示入口。公开提供试玩前应增加访问控制和调用额度。

## 限制与来源

后端以单个 worker 运行；初始建档使用进程内锁，已有存档使用 PowerContext ETag 条件更新。本作品不承诺多副本初始建档一致性或生产级多租户身份管理。每位玩家最多10周目。模型台词可能变化，正式分数、道具与结局以状态面板为准。

游戏、界面与适配代码采用 Apache-2.0。PowerContext 运行时从其官方固定提交安装，不重复分发上游源码与安装包。Dify DSL 按官方 1.9.1 节点格式自行编排，没有复制其他娱乐作品。

- [赛题要求](https://oceanbase.yuque.com/org-wiki-obtech-vh7w9r/hc3pio/lhfslihtnnb8yqh6)
- [Dify 1.9.1](https://github.com/langgenius/dify/tree/1.9.1)
- [PowerContext](https://github.com/oceanbase/powercontext)
- [PowerContext HTTP 接口](https://powercontext.oceanbase.io/zh/docs/develop/interfaces/)

提交时使用竞赛分支中包含完整资产和真实证据的最新 commit hash。平台侧仓库权限和授权条件按赛事要求核对。
