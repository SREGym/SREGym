# 项目整体结构注释示例
# 在以下示例中，我们使用 Python 注释的风格，逐层解释各模块的职责和文件组织

# ===== Generators（故障注入与负载生成） =====
# generators/fault:
#   该目录下定义各种级别的故障注入器（fault injection），
#   从基础抽象到应用层、虚拟层等具体实现。
#   - base.py         # 抽象基类，定义注入接口和公共逻辑
#   - inject_app.py   # 在应用容器中注入故障（如进程杀死、崩溃）
#   - inject_virtual.py  # 在虚拟层（网络、IO）注入延迟或丢包
#   # …etc

# generators/workload:
#   该目录下负责生成压力或流量，模拟真实业务场景。
#   - wrk.py          # 与 wrk 压力测试工具对接，启动并收集压力测试结果


# ===== Conductor（编排与评估引擎） =====
# conductor/conductor.py:
#   核心执行逻辑，协调 Agent 与测试环境的交互，
#   包括：环境准备、NOOP 基线测试、故障注入、检测/定位/缓解评估、清理收尾。

# conductor/problems:
#   定义不同的“实验场景”或“题目”，每个场景封装应用部署与故障接口。
#   - base.py         # 抽象基类，定义部署、注入、恢复、清理接口
#   - noop.py         # 无故障场景，仅用于基线验证 Agent 不报错
#   - registry.py     # 将场景 ID 映射到具体类，便于动态加载
#   - helpers.py      # 公共工具，例如获取前端 URL

# conductor/oracles:
#   实现分阶段评估逻辑的“预言机”（Oracle），为 Agent 提交提供标准答案。
#   - detection.py    # 判断 Agent 是否正确检测出了故障
#   - localization.py # 验证 Agent 是否指出了故障的具体组件或位置
#   - mitigation.py   # 检查 Agent 提出的缓解或修复方案是否有效


# ===== Service（集群与应用接口） =====
# service/apps:
#   每个子目录代表一个被测应用，封装其部署、删除、重启、配置逻辑。

# service/helm.py
#   提供对 Helm chart 的封装，用于批量部署或升级应用

# service/kubectl.py
#   对 kubectl 命令行的封装，简化对 Kubernetes 集群的操作

# service/shell.py
#   通用 shell 命令接口，可用于执行其他系统命令

# service/metadata:
#   存放各应用所需的配置文件、镜像地址、端口映射等元数据

# service/telemetry:
#   轻量级日志和指标采集库，用于在 Agent 或环境内部记录数据


# ===== Observer（观测与数据存储） =====
# observer/filebeat:
#   Filebeat 配置，用于收集容器或节点日志，转发给 Logstash 或 Elasticsearch

# observer/logstash:
#   Logstash 配置，用于日志过滤、格式化并持久化

# observer/prometheus:
#   Prometheus 部署配置，用于实时指标采集与监控

# observer/log_api.py, metric_api.py, trace_api.py:
#   提供轻量级 API，将采集到的日志、指标和追踪数据写入本地存储，
#   方便离线分析和调试。
