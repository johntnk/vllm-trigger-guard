# vLLM Trigger Guard

[English](README.en.md) · [完整实验报告](docs/REPORT.zh-CN.md) · [安装与集成](docs/INSTALL.md)

一个实验性的模型输出禁词拦截实现，包含纯 CPU 文本检查器、vLLM 0.17.0 的 MTP 验证后检查补丁，以及有界排队的流式 HTTP 适配器。

**行为：命中配置中的精确字符串时，阻止补全该词的内容及后续续写，结束生成，并追加一次固定提示。已经发布的词前缀可能保留。** 例如禁词为 `花朵`，客户端可能看到 `花` 加提示，完整的 `花朵` 不会由该检查路径放行。这不是整词脱敏、语义审核或任意输入下的安全保证。

项目独立于 vLLM，不是官方组件。当前发布为实验版本；GitHub Actions 运行 CPU 和模拟 HTTP 测试，不运行模型推理。

## 快速体验：不需要模型或显卡

```bash
git clone https://github.com/johntnk/vllm-trigger-guard.git
cd vllm-trigger-guard
python -m venv .venv
# Linux / macOS
source .venv/bin/activate
# Windows PowerShell 使用 .venv\Scripts\Activate.ps1
python -m pip install -e ".[test]"
python -m trigger_guard demo
python -m unittest discover -s tests -v
```

默认示例将 `SECRET` 配置为禁词，输出中可能保留 `SECRE`，随后只发送一次提示。示例不会调用 vLLM、下载模型或占用 GPU。

使用自己的词表：

```bash
python -m trigger_guard demo --policy examples/policy.json --text "这里有花朵和更多内容" --chunk-size 1
```

## 两层检查

1. **验证后的输出单元检查**：MTP 草稿经过引擎验证后，检查真正可交付的输出，命中时不提交补全禁词的单元。
2. **最终文本检查**：检查实际解码文本，覆盖不同分词方式和词内子串。完整方案必须保留这一层。

规则提前构建索引，同进程内共享只读程序；每个请求维护自己的进度。大词表使用 Aho–Corasick 多模式匹配。它是已有算法，项目的工作是适配、实现与验证。

本项目复用了原生 `bad_words` 的部分编码思路，**没有启用原生 `bad_words` 概率屏蔽**。其行为也不等同于原生 `stop`：本项目允许已发出的禁词前缀保留，并追加服务器提示。

## 已有实验结果

以下是打包前、固定版本实验的历史结果；不代表这次开源包重新完成了 GPU 验证。

| 项目 | 已测结果与条件 |
| --- | --- |
| 标准编码回放 | 1,004,968次应拦截机会全部拦截，包含不同规模与重复回放 |
| 替代编码 | 138个正例由最终文本检查捕获；仅编码匹配不足以覆盖 |
| 真实模型负例 | 3,284个完整返回负例未出现策略提示 |
| 词表容量 | 常规和共同前缀词表到10万条；长短语到3.5万条 |
| 词条长度 | 最长成功验证156字符；4096字符只是配置允许上限 |
| 热运行延迟 | 16条→10万条，短请求首字P95约373→388毫秒；每组32请求 |
| 短请求并发 | 16并发组全部完成；32/64并发有大量准入拒绝 |
| 一小时运行 | 10万规则、引擎准入4、约0.42请求/秒；1515请求中1494完整返回、21主动断连 |
| 资源 | 服务RSS合计约4.05 GiB；常规10万规则CPU测试进程峰值1.450 GiB，均非索引净增内存 |

没有与原生 `stop` 做同条件性能比较，不能据此宣称比原生实现更快。历史GPU索引越界原因仍未完全确定；复测使用了已合并上游的GDN修复。[完整报告](docs/REPORT.zh-CN.md)给出分母、配置、失败记录和适用范围。

## vLLM / MTP 集成

集成固定到 **vLLM 0.17.0 / `b31e9326a7d9394aab8c767f8ebe225c65594b60`**，面向 Linux、单张 CUDA 显卡、单个 API 进程。它通过校验源码并复制出独立 overlay 应用补丁，不覆盖已安装的 vLLM。新版 vLLM 未验证，不能直接套用。

按[安装说明](docs/INSTALL.md)配置独立虚拟环境、模型和 overlay 后，启动形式如下：

```bash
CUDA_VISIBLE_DEVICES=0 python -m trigger_guard serve \
  --overlay .runtime/vllm017 \
  --engine-config examples/engine.json \
  --policy examples/policy.json \
  --run-dir runs/example-001
```

请先把 `examples/engine.json` 的模型路径改为自己拥有的本地模型。示例 MTP 配置要求模型本身支持 MTP。适配器只监听 `127.0.0.1:18871`，提供 `/v1/completions`；没有认证，不是可直接暴露到公网的服务。

## Python 文本检查接口

```python
from trigger_guard import EgressProgram, Publication, load_policy

policy = load_policy("examples/policy.json")  # 包含词表及提示边界验证
program = EgressProgram(policy["rules"])      # 为多个请求共享
request = Publication(policy, "egress", program)  # 每个请求单独创建

print(request.process("这里有花"))
print(request.process("朵和更多内容"))
```

这是第二层检查的独立接口，不应把它单独描述为完整的 MTP 引擎集成。多条规则同时完成时，按 `priority`、`rule_id` 确定规则；文字按 Unicode 码点精确匹配，不自动处理同义词、大小写或字符规范化。

## 项目内容

- `src/trigger_guard/`：匹配器、发布状态机、有界适配器和命令入口。
- `src/trigger_guard/patches/`：固定版本补丁、原始/目标文件指纹。
- `tests/`：独立参考判定器、随机回放、跨段匹配、取消竞态、真实 TCP 的模拟引擎测试。
- `benchmarks/`：合成词表和显式启动的压测工具；`results/` 仅包含历史汇总。
- `docs/`：安装、架构、适用范围、验证记录和完整中文报告。
- `PROVENANCE.json`、`NOTICE`：实现来源、导出变更和上游归属。

模型权重、原始逐请求日志、机器地址、运行环境、凭据和业务词表不随仓库发布。

## 限制与许可

当前不支持多 GPU、多 API worker、工具调用、多模态审核、动态多租户词表，也限制了 `n>1`、logprobs、结构化输出及其他采样参数组合。没有生产容量或可用性承诺。详细内容见[架构与限制](docs/ARCHITECTURE.md)。

[Apache-2.0](LICENSE)。GDN修复归属原上游[PR #34871](https://github.com/vllm-project/vllm/pull/34871)。实现、文档、打包和实验使用了 AI 辅助。
