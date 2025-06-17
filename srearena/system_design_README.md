<small>

## Generators（故障注入与负载生成）

- **generators/fault**  
  - `base.py`：抽象基类，定义注入接口和公共逻辑  
  - `inject_app.py`：在应用容器中注入故障（如进程杀死、崩溃）  
  - `inject_virtual.py`：在虚拟层（网络、IO）注入延迟或丢包  
  - …等其他故障注入实现

- **generators/workload**  
  - `wrk.py`：与 wrk 压力测试工具对接，启动并收集压力测试结果


## Conductor（编排与评估引擎）

- **conductor/conductor.py**  
  核心执行流程，协调 Agent 与测试环境交互：环境准备 → NOOP 基线测试 → 故障注入 → 检测/定位/缓解评估 → 清理。

- **conductor/problems**  
  定义各实验场景，每个场景封装应用部署与故障接口：  
  - `base.py`：场景抽象基类，定义部署、注入、恢复、清理接口  
  - `noop.py`：无故障场景，仅用于基线验证 Agent 不报错  
  - `registry.py`：将场景 ID 映射到具体类，便于动态加载  
  - `helpers.py`：公共工具（如获取前端 URL）

- **conductor/oracles**  
  分阶段评估“预言机”（Oracle），提供标准答案：  
  - `detection.py`：判断 Agent 是否正确检测出了故障  
  - `localization.py`：验证 Agent 是否指出故障具体组件或位置  
  - `mitigation.py`：检查 Agent 提出的缓解或修复方案是否有效


## Service（集群与应用接口）

- **service/apps**  
  每个子目录对应一个被测应用，封装其部署、删除、重启、配置逻辑。

- **service/helm.py**  
  Helm chart 封装，用于批量部署或升级应用。

- **service/kubectl.py**  
  kubectl 命令行封装，简化对 Kubernetes 集群的操作。

- **service/shell.py**  
  通用 Shell 命令接口，可执行其他系统命令。

- **service/metadata**  
  存放应用所需的配置文件、镜像地址、端口映射等元数据。

- **service/telemetry**  
  轻量级日志与指标采集库，用于在 Agent 或环境内部记录数据。


## Observer（观测与数据存储）

- **observer/filebeat**  
  Filebeat 配置，收集容器或节点日志并转发。

- **observer/logstash**  
  Logstash 配置，过滤、格式化并持久化日志。

- **observer/prometheus**  
  Prometheus 部署配置，实时采集与监控指标。

- **observer/log_api.py**, `metric_api.py`, `trace_api.py`  
  轻量级 API，将日志、指标和追踪数据写入本地存储，便于离线分析和调试。

</small>
