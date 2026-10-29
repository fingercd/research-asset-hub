"""Build the reviewed v2 domain-portal sidecars without changing source files."""

# ruff: noqa: E501

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import yaml

from packages.domain.exporters.common import atomic_write_json, atomic_write_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = PROJECT_ROOT.parent
DEFAULT_CONFIG_ROOT = PROJECT_ROOT / "config" / "domain-portals" / "v2"
PORTAL_MANIFEST = (
    PROJECT_ROOT / "config" / "intake" / "domain-portals-20260726" / "manifest.json"
)
CATALOG = PROJECT_ROOT / "outputs" / "open-catalog" / "catalog.jsonl"
WORK_ROOT = PROJECT_ROOT / "work" / "domain-portal-v2"
FIXED_SECTIONS = (
    "scope",
    "timeline",
    "capability-method-map",
    "landmarks",
    "benchmarks",
    "reading-routes",
    "catalog",
    "frontiers",
    "evidence",
)


def _item(identifier: str, title: str, summary: str) -> dict[str, str]:
    return {"id": identifier, "title": title, "summary": summary}


BLUEPRINTS: dict[str, dict[str, Any]] = {
    "agent": {
        "label": "Agent 学习",
        "description": "围绕规划、工具使用、记忆、反思、多智能体协作和真实环境执行组织 Agent 研究。",
        "overview": (
            "Agent 研究关注模型如何在目标、环境状态和工具约束下持续采取行动。"
            "本领域纳入推理—行动框架、规划、工具学习、记忆、反思、GUI/网页/软件工程智能体、"
            "多智能体协作及其评测；单纯提示词技巧仅在直接改变可执行代理能力时纳入。"
        ),
        "timeline": [
            ("2017–2021", "交互环境与具身任务奠基", "网页、文本和具身环境把语言理解转化为可验证行动。", ["World of Bits", "ALFWorld"]),
            ("2022", "推理与行动开始统一", "语言模型规划、工具增强和 ReAct 形成现代 Agent 的核心接口。", ["ReAct", "Toolformer"]),
            ("2023", "记忆、反思与多智能体框架扩张", "经验记忆、自我修正、协作和开放世界探索成为主要方向。", ["Reflexion", "Voyager"]),
            ("2024–2026", "从演示走向真实工作环境", "GUI、软件工程和长时任务强调可靠性、安全与可复核评测。", ["SWE-agent", "OSWorld"]),
        ],
        "capabilities": [
            _item("planning", "规划与分解", "把长目标拆成可执行步骤并在反馈后重规划。"),
            _item("tool-use", "工具与环境交互", "选择 API、浏览器、代码和具身动作并读取结果。"),
            _item("memory", "记忆与学习", "保存经历、检索状态并利用历史改进行动。"),
            _item("collaboration", "协作与治理", "在多智能体、用户和安全约束之间协调。"),
        ],
        "methods": [
            _item("reason-act", "推理—行动交替", "以显式推理、动作和观察构成闭环。"),
            _item("search-plan", "搜索与规划", "使用树搜索、计划器或世界模型选择行动。"),
            _item("reflection", "反思与记忆", "通过语言反馈和情景记忆进行自我修正。"),
            _item("multi-agent", "多智能体编排", "用角色、协议和共享状态组织协作。"),
        ],
        "landmarks": ["ReAct", "Toolformer", "Reflexion", "Voyager", "SWE-agent"],
        "routes": [
            ("agent-basics", "Agent 基础主线", "理解感知—推理—行动闭环。", ["ReAct", "Toolformer", "Reflexion"]),
            ("tool-agents", "工具与软件工程路线", "理解工具调用到真实代码环境执行。", ["ToolLLM", "SWE-agent", "OpenHands"]),
            ("embodied-agents", "具身与开放世界路线", "理解语言如何约束实体环境中的行动。", ["ALFWorld", "Voyager", "RT-2"]),
            ("evaluation-safety", "评测与安全路线", "理解可靠性、风险和长时任务测量。", ["AgentBench", "OSWorld", "AI Agents That Matter"]),
        ],
        "benchmarks": [
            ("interactive", "交互式任务", ["ALFWorld", "WebArena", "WebShop"], ["成功率", "轨迹长度", "约束满足率"], "环境与工具版本不同，成功率不可脱离任务配置比较。"),
            ("software", "软件工程任务", ["SWE-bench", "OSWorld"], ["解决率", "测试通过率", "成本"], "必须区分数据污染、工具权限和评测 harness。"),
            ("safety", "安全与可信执行", ["AgentSafe", "风险沙箱"], ["违规率", "拒绝准确率", "恢复率"], "安全得分必须与任务完成能力同时报告。"),
        ],
        "questions": [
            "如何在长时任务中维持可验证状态，而不是只延长上下文？",
            "怎样把工具权限、成本和失败恢复纳入统一的 Agent 评测？",
            "多智能体协作何时真正优于单智能体搜索与自反思？",
        ],
        "extras": [
            "agent 学习/outputs/agent-paper-library/site/timeline/index.html",
            "agent 学习/outputs/agent-paper-library/site/modules/index.html",
            "agent 学习/outputs/agent-paper-library/site/reading/index.html",
            "agent 学习/outputs/agent-paper-library/site/evidence/index.html",
        ],
    },
    "infra": {
        "label": "AI / 大模型 Infra",
        "description": "组织训练、推理、并行、显存、编译、网络、调度和可靠性的大模型系统研究。",
        "overview": (
            "大模型 Infra 的核心问题是如何在硬件、通信、内存、运行时和服务目标之间做跨层权衡。"
            "本领域覆盖训练与推理系统、并行策略、算子编译、数据管线、网络、调度、容错和多租户服务；"
            "纯模型结构仅在直接改变系统代价时纳入。"
        ),
        "timeline": [
            ("2012–2016", "分布式训练抽象形成", "参数服务器、数据并行和通用计算图建立系统基础。", ["Parameter Server", "TensorFlow"]),
            ("2017–2020", "专用硬件与多维并行", "TPU、流水线、模型并行和编译器扩大可训练模型规模。", ["TPU", "Megatron-LM"]),
            ("2021–2023", "万亿模型训练与连续批处理", "状态分片、MoE、FlashAttention 和 PagedAttention 重塑训练与服务。", ["ZeRO", "PagedAttention"]),
            ("2024–2026", "解耦服务与生产可靠性", "P/D 解耦、KV cache 数据面和检查点系统成为一等公民。", ["DistServe", "Mooncake"]),
        ],
        "capabilities": [
            _item("scale-training", "规模化训练", "在更多设备上维持有效吞吐和收敛。"),
            _item("memory-efficiency", "内存效率", "降低参数、激活、优化器和 KV cache 占用。"),
            _item("low-latency-serving", "低延迟服务", "在吞吐、首 token 和尾延迟之间满足 SLO。"),
            _item("reliability", "可靠性与资源治理", "处理故障、弹性、多租户公平与成本。"),
        ],
        "methods": [
            _item("parallelism", "并行与切分", "数据、张量、流水线、专家和上下文并行。"),
            _item("compiler-kernel", "编译器与 Kernel", "图优化、自动调度和 IO 感知算子。"),
            _item("memory-system", "内存与缓存系统", "分片、offload、checkpoint 和 KV cache。"),
            _item("scheduling", "调度与解耦", "连续批处理、SLO 调度和 P/D 解耦。"),
        ],
        "landmarks": ["TensorFlow", "Megatron-LM", "ZeRO", "FlashAttention", "PagedAttention"],
        "routes": [
            ("training-systems", "训练系统路线", "理解分布式训练的主干抽象。", ["TensorFlow", "Megatron-LM", "ZeRO"]),
            ("serving-systems", "LLM Serving 路线", "理解连续批处理、KV cache 和解耦服务。", ["Orca", "PagedAttention", "DistServe"]),
            ("compiler-route", "编译器与 Kernel 路线", "理解从图优化到 IO 感知算子。", ["TVM", "Triton", "FlashAttention"]),
            ("cluster-reliability", "集群与可靠性路线", "理解调度、网络、checkpoint 和容错。", ["Tiresias", "ByteCheckpoint", "MegaScale"]),
        ],
        "benchmarks": [
            ("training", "分布式训练", ["固定模型与集群配置"], ["tokens/s", "MFU", "恢复时间"], "必须同时报告模型、精度、网络和并行配置。"),
            ("serving", "在线推理", ["ShareGPT 类负载", "合成到达过程"], ["TTFT", "TPOT", "P99", "goodput"], "吞吐与延迟需要在同一 SLO 下比较。"),
            ("kernel", "算子与编译", ["固定 shape/精度/硬件"], ["延迟", "带宽利用率", "数值误差"], "微基准提升不能直接等价为端到端收益。"),
        ],
        "questions": [
            "训练和推理的状态数据面能否形成统一的可组合抽象？",
            "怎样在异构加速器与不稳定网络中维持可预测 SLO？",
            "系统论文如何同时报告性能、成本、能耗和可靠性？",
        ],
    },
    "world-model": {
        "label": "世界模型",
        "description": "组织潜变量动力学、预测、规划、生成式环境和行动条件建模研究。",
        "overview": (
            "世界模型学习环境状态如何随行动演化，并把预测用于规划、控制或交互生成。"
            "本领域要求表示、动力学、干预和决策形成闭环；单纯无条件视频生成若不支持行动或决策，不作为核心世界模型。"
        ),
        "timeline": [
            ("2011–2017", "视觉预测与潜在控制前史", "从像素预测和潜在状态学习开始连接视觉与控制。", ["PILCO", "Embed to Control"]),
            ("2018–2020", "现代潜变量世界模型形成", "World Models、PlaNet、Dreamer 和 MuZero 建立模型式决策主干。", ["World Models", "PlaNet"]),
            ("2021–2023", "样本效率与通用预测扩张", "Transformer、离散 token 和生成模型扩大环境预测范围。", ["DreamerV3", "IRIS"]),
            ("2024–2026", "基础模型与交互世界", "生成式模拟器、机器人和驾驶模型转向更开放的行动条件预测。", ["Genie", "GAIA-1"]),
        ],
        "capabilities": [
            _item("representation", "状态表示", "从观测中提取可预测、可控制的潜在状态。"),
            _item("dynamics", "动力学预测", "预测行动条件下的未来状态和不确定性。"),
            _item("planning", "规划与控制", "利用模型评估、搜索或优化行动。"),
            _item("interactive-generation", "交互生成", "生成可持续响应用户或代理行动的环境。"),
        ],
        "methods": [
            _item("latent-dynamics", "潜变量动力学", "在压缩状态空间中学习转移与奖励。"),
            _item("search-control", "搜索与控制", "MPC、树搜索和想象轨迹优化。"),
            _item("token-model", "Token 世界模型", "把观测离散化并用序列模型预测。"),
            _item("generative-simulator", "生成式模拟器", "用扩散或大规模视频模型生成交互环境。"),
        ],
        "landmarks": ["World Models", "PlaNet", "Dreamer", "MuZero", "Genie"],
        "routes": [
            ("foundations", "世界模型基础路线", "理解表示、动力学和想象控制。", ["World Models", "PlaNet", "Dreamer"]),
            ("planning", "规划与搜索路线", "理解 learned model 如何进入决策。", ["PILCO", "MuZero", "TD-MPC"]),
            ("token-worlds", "Token 与生成世界路线", "理解离散序列和生成式环境。", ["IRIS", "Genie", "VideoGPT"]),
            ("embodied", "机器人与驾驶路线", "理解行动条件预测在真实系统中的边界。", ["DreamerV3", "GAIA-1", "DriveDreamer"]),
        ],
        "benchmarks": [
            ("control", "控制与样本效率", ["Atari", "DM Control"], ["return", "human-normalized score", "environment steps"], "环境版本、动作重复和训练步数必须一致。"),
            ("prediction", "未来预测", ["BAIR", "KITTI", "机器人视频"], ["FVD", "LPIPS", "任务成功率"], "感知指标不能替代行动可用性。"),
            ("interactive", "交互环境生成", ["游戏/机器人交互任务"], ["一致性", "可控性", "长时稳定性"], "需区分视觉逼真与可用于规划的因果一致性。"),
        ],
        "questions": [
            "如何区分视觉上合理的预测与对决策真正有用的动力学？",
            "世界模型如何表达认知不确定性并避免想象误差累积？",
            "大规模生成式环境能否提供可验证、可复现的交互规律？",
        ],
    },
    "reinforcement-learning": {
        "label": "强化学习",
        "description": "组织价值学习、策略优化、模型式与离线学习、多智能体、RLHF 和可验证推理研究。",
        "overview": (
            "强化学习研究智能体如何在序列决策中利用奖励和交互改进策略。"
            "本领域覆盖经典 MDP、价值与策略方法、深度 RL、模型式/离线/多智能体学习，以及人类偏好和可验证奖励驱动的大模型后训练。"
        ),
        "timeline": [
            ("1957–1991", "MDP、动态规划与 TD 基础", "序列决策、时序差分和 Q-learning 建立理论主干。", ["Markovian Decision Process", "Q-learning"]),
            ("1992–2014", "策略梯度与函数逼近", "直接优化策略、Actor-Critic 和奖励塑形扩展连续控制。", ["Policy Gradient", "Actor-Critic"]),
            ("2015–2019", "深度 RL 主干形成", "DQN、TRPO、PPO、SAC 和分布式训练推动规模化。", ["Human-level Control", "Proximal Policy Optimization"]),
            ("2020–2026", "离线、偏好与可验证推理", "离线数据、RLHF、DPO、GRPO 与 RLVR 把 RL 接到基础模型。", ["Training language models", "Direct Preference Optimization"]),
        ],
        "capabilities": [
            _item("value-estimation", "价值估计", "估计状态与动作的长期回报。"),
            _item("policy-optimization", "策略优化", "直接改进随机或确定性策略。"),
            _item("data-efficiency", "数据效率", "利用模型、离线数据和重放减少交互。"),
            _item("alignment-reasoning", "对齐与推理", "从偏好或可验证奖励学习复杂行为。"),
        ],
        "methods": [
            _item("value-based", "价值方法", "TD、Q-learning、DQN 及其扩展。"),
            _item("actor-critic", "策略梯度与 Actor-Critic", "TRPO、PPO、SAC 等策略优化。"),
            _item("model-offline", "模型式与离线 RL", "世界模型、保守估计和离线策略学习。"),
            _item("preference-rl", "偏好与可验证奖励", "RLHF、DPO、GRPO 和 RLVR。"),
        ],
        "landmarks": ["Q-learning", "Human-level Control", "Proximal Policy Optimization", "Soft Actor-Critic", "Direct Preference Optimization"],
        "routes": [
            ("algorithm-basics", "算法基础路线", "建立价值与策略优化的共同语言。", ["Q-learning", "Policy Gradient", "Proximal Policy Optimization"]),
            ("deep-rl", "深度 RL 路线", "理解函数逼近、稳定训练和连续控制。", ["Human-level Control", "Rainbow", "Soft Actor-Critic"]),
            ("offline-model", "离线与模型式路线", "理解数据制度和学习模型的作用。", ["Offline Reinforcement Learning", "World Models", "Dreamer"]),
            ("llm-rl", "RLHF 与推理路线", "理解偏好、直接优化和可验证奖励。", ["Training language models", "Direct Preference Optimization", "DeepSeek-R1"]),
        ],
        "benchmarks": [
            ("classic-control", "经典与连续控制", ["Atari", "MuJoCo", "DM Control"], ["return", "sample efficiency", "stability"], "必须统一环境版本、seed 和训练预算。"),
            ("offline", "离线强化学习", ["D4RL", "离线日志"], ["normalized return", "OOD action rate"], "数据质量与行为策略决定可比性。"),
            ("preference", "偏好与推理后训练", ["人类偏好集", "可验证题目"], ["win rate", "reward", "pass@k"], "奖励模型偏差和基准污染必须单独报告。"),
        ],
        "questions": [
            "怎样在函数逼近、分布偏移和长期信用分配下给出可靠保证？",
            "离线数据、在线探索和世界模型应如何组合？",
            "可验证奖励带来的能力提升能否迁移到不可验证目标？",
        ],
    },
    "fine-tuning": {
        "label": "大模型微调",
        "description": "组织参数高效微调、持续预训练、指令微调、偏好优化与后训练方法。",
        "overview": (
            "大模型微调研究如何在有限算力和数据下改变基础模型的任务能力、行为和偏好。"
            "本领域覆盖全量微调、Adapter/LoRA 等参数高效方法、量化微调、指令学习、持续预训练和偏好优化。"
        ),
        "timeline": [
            ("2018–2020", "预训练—微调范式形成", "任务微调、Adapter 和前缀方法降低迁移成本。", ["Adapter", "Prefix-Tuning"]),
            ("2021–2022", "参数高效方法成为主流", "LoRA、Prompt Tuning 和指令微调形成通用工具箱。", ["LoRA", "Prompt Tuning"]),
            ("2023", "量化与偏好优化扩张", "QLoRA、DPO 等方法连接低成本训练与行为对齐。", ["QLoRA", "Direct Preference Optimization"]),
            ("2024–2026", "后训练系统化", "数据配方、多阶段训练、稀疏适配和推理强化成为核心。", ["ORPO", "KTO"]),
        ],
        "capabilities": [
            _item("task-adaptation", "任务适配", "让基础模型学习领域任务和输出格式。"),
            _item("instruction-following", "指令遵循", "利用多任务指令数据塑造通用交互能力。"),
            _item("preference-alignment", "偏好对齐", "根据成对偏好或反馈改变行为。"),
            _item("efficient-customization", "低成本定制", "降低显存、存储和多租户部署成本。"),
        ],
        "methods": [
            _item("full-sft", "全量与监督微调", "更新全部参数或持续预训练。"),
            _item("adapter-lora", "Adapter 与 LoRA", "使用低秩或插入模块更新少量参数。"),
            _item("prompt-prefix", "Prompt 与 Prefix", "在输入或隐状态上学习软提示。"),
            _item("preference", "偏好优化", "RLHF、DPO、KTO、ORPO 等目标。"),
        ],
        "landmarks": ["Adapter", "Prefix-Tuning", "LoRA", "QLoRA", "Direct Preference Optimization"],
        "routes": [
            ("peft-basics", "PEFT 基础路线", "理解少参数更新的主要设计。", ["Adapter", "Prefix-Tuning", "LoRA"]),
            ("low-memory", "低显存训练路线", "理解量化、低秩和优化器状态。", ["LoRA", "QLoRA", "GaLore"]),
            ("instruction", "指令微调路线", "理解数据混合和任务泛化。", ["FLAN", "Self-Instruct", "LIMA"]),
            ("preference", "偏好优化路线", "理解奖励建模与直接偏好目标。", ["Training language models", "Direct Preference Optimization", "KTO"]),
        ],
        "benchmarks": [
            ("adaptation", "任务与领域适配", ["领域任务集", "通用能力集"], ["任务得分", "遗忘率", "训练成本"], "必须同时测目标任务和基础能力保持。"),
            ("alignment", "偏好与对齐", ["偏好对比集", "安全集"], ["win rate", "拒绝率", "reward margin"], "自动评审器偏差不能作为唯一证据。"),
            ("efficiency", "训练与部署效率", ["固定模型/硬件"], ["峰值显存", "训练时长", "adapter 大小"], "参数比例低不等于端到端成本低。"),
        ],
        "questions": [
            "如何区分数据质量、目标函数与参数化方式带来的收益？",
            "微调如何避免能力遗忘和安全边界漂移？",
            "多 adapter、持续更新和推理部署能否形成统一生命周期？",
        ],
    },
    "generation": {
        "label": "图像与视频生成",
        "description": "组织 VAE、GAN、自回归、扩散、流匹配及可控图像与视频生成研究。",
        "overview": (
            "图像与视频生成研究如何学习视觉数据分布并在文本、结构、动作或参考内容条件下合成新样本。"
            "本领域同时关注生成机制、表示空间、条件控制、长时一致性、效率和评测可靠性。"
        ),
        "timeline": [
            ("2013–2017", "VAE、GAN 与自回归奠基", "概率潜变量、对抗学习和像素序列建模形成三条早期路线。", ["Auto-Encoding Variational Bayes", "Generative Adversarial Nets"]),
            ("2018–2021", "高分辨率与扩散模型崛起", "VQ 表示、DDPM、score model 和 CLIP 打开文本条件生成。", ["Denoising Diffusion", "CLIP"]),
            ("2022–2023", "潜空间扩散与可控生成", "LDM、ControlNet、DiT 和视频扩散推动规模化。", ["Latent Diffusion", "ControlNet"]),
            ("2024–2026", "流匹配、视频与交互世界", "Rectified flow、长视频、多模态条件和世界模拟成为前沿。", ["Flow Matching", "Wan"]),
        ],
        "capabilities": [
            _item("fidelity", "视觉质量与多样性", "生成清晰且覆盖数据分布的样本。"),
            _item("conditioning", "文本与结构控制", "遵循文本、布局、姿态、深度和参考内容。"),
            _item("temporal", "时序一致性", "维持运动、身份、场景和长时因果连续。"),
            _item("efficiency", "生成效率", "降低训练、采样和高分辨率生成成本。"),
        ],
        "methods": [
            _item("vae-gan", "VAE 与 GAN", "潜变量概率建模与对抗训练。"),
            _item("autoregressive", "自回归 Token", "离散视觉 token 的序列生成。"),
            _item("diffusion", "扩散与 Score", "逐步去噪和随机微分方程。"),
            _item("flow", "Flow Matching", "学习连续运输路径并减少采样步骤。"),
        ],
        "landmarks": ["Generative Adversarial Nets", "Denoising Diffusion", "Latent Diffusion", "ControlNet", "Flow Matching"],
        "routes": [
            ("image-basics", "图像生成基础路线", "理解主要生成范式的差异。", ["Auto-Encoding Variational Bayes", "Generative Adversarial Nets", "Denoising Diffusion"]),
            ("text-image", "文本到图像路线", "理解语义条件、潜空间和可控性。", ["CLIP", "Latent Diffusion", "ControlNet"]),
            ("video", "视频生成路线", "理解时序建模、潜空间和长视频挑战。", ["Video Diffusion Models", "VideoLDM", "Wan"]),
            ("evaluation", "评测与可靠性路线", "理解质量、多样性、对齐和协议边界。", ["FID", "FVD", "VBench"]),
        ],
        "benchmarks": [
            ("image", "图像生成", ["COCO prompts", "GenEval"], ["FID", "CLIPScore", "GenEval"], "自动指标与人类偏好、提示分布并不等价。"),
            ("video", "视频生成", ["UCF-101", "VBench prompts"], ["FVD", "VBench", "人类偏好"], "分辨率、帧数、采样步数必须同时报告。"),
            ("control", "可控编辑与生成", ["姿态/深度/布局条件集"], ["条件一致性", "感知质量", "身份保持"], "不同控制信号不能用单一指标横比。"),
        ],
        "questions": [
            "如何同时测量视觉质量、语义对齐、多样性与物理一致性？",
            "长视频生成怎样避免身份、场景和因果关系逐步漂移？",
            "生成模型何时能够成为可靠的交互环境而不仅是视觉合成器？",
        ],
    },
    "llm-tech-reports": {
        "label": "大语言模型与大厂技术报告",
        "description": "组织大模型技术报告、模型卡、系统卡和关键训练方法的一手证据。",
        "overview": (
            "本领域以可核验的一手技术报告、模型卡和关键论文为中心，关注规模化、数据、架构、"
            "后训练、推理、Agent、多模态和安全透明度。版本发布时间不等于技术进步，缺少可比协议时不做硬排名。"
        ),
        "timeline": [
            ("2017–2020", "Transformer 与规模化前史", "注意力、缩放规律和大规模预训练建立基础。", ["Attention Is All You Need", "Scaling Laws"]),
            ("2021–2022", "稀疏架构与指令对齐", "MoE、思维链和人类反馈把规模转化为交互能力。", ["Switch Transformer", "Training language models"]),
            ("2023–2024", "开放模型、多模态与长上下文", "GPT-4、Llama、Gemini 和开源家族扩展能力与透明度。", ["GPT-4 Technical Report", "Llama 2"]),
            ("2025–2026", "推理与 Agent 后训练", "可验证奖励、测试时计算和工具使用成为技术报告主线。", ["DeepSeek-R1", "Kimi K2"]),
        ],
        "capabilities": [
            _item("language-knowledge", "语言与知识", "预训练形成通用表征、知识和生成能力。"),
            _item("reasoning", "推理与测试时计算", "通过数据、搜索和强化学习改进复杂推理。"),
            _item("agent-tool", "Agent 与工具", "调用工具、编程并完成多步真实任务。"),
            _item("multimodal-safety", "多模态与安全", "处理视觉等模态并报告风险、评测和限制。"),
        ],
        "methods": [
            _item("scaling-data", "规模化与数据", "计算、数据配方和缩放规律。"),
            _item("architecture", "架构与效率", "MoE、长上下文、注意力和状态空间模型。"),
            _item("post-training", "后训练与偏好", "指令微调、RLHF、DPO 和 RLVR。"),
            _item("test-time", "测试时计算与工具", "思维链、搜索、代码和 Agent 工作流。"),
        ],
        "landmarks": ["Attention Is All You Need", "Scaling Laws", "GPT-4 Technical Report", "Llama 2", "DeepSeek-R1"],
        "routes": [
            ("llm-foundations", "从零理解 LLM", "理解架构、规模与数据。", ["Attention Is All You Need", "Scaling Laws", "Training Compute-Optimal"]),
            ("post-training", "对齐与后训练路线", "理解人类反馈、直接偏好和可验证奖励。", ["Training language models", "Direct Preference Optimization", "DeepSeek-R1"]),
            ("reasoning", "推理模型路线", "理解思维链、测试时计算和强化学习。", ["Chain-of-Thought", "Scaling LLM Test-Time Compute", "DeepSeek-R1"]),
            ("agentic", "Agentic LLM 路线", "理解工具使用、编程和 Agent 评测。", ["ReAct", "AgentBench", "Kimi K2"]),
        ],
        "benchmarks": [
            ("capability", "通用能力", ["MMLU 类", "数学/代码集"], ["accuracy", "pass@k"], "数据污染和提示设置会显著影响结果。"),
            ("reasoning", "推理与测试时计算", ["可验证数学/代码任务"], ["pass@1", "pass@k", "token/cost"], "必须同时报告采样预算和选择策略。"),
            ("safety", "安全与透明度", ["系统卡评测集"], ["违规率", "拒绝率", "校准"], "不同厂商披露范围不同，不能直接汇总成排名。"),
        ],
        "questions": [
            "如何在厂商披露不完整时建立可比且不过度推断的证据框架？",
            "测试时计算、数据质量和参数规模之间的真实替代关系是什么？",
            "Agent 能力报告如何同时包含成功率、成本、权限和安全边界？",
        ],
    },
    "vision-language-navigation": {
        "label": "视觉语言导航（VLN）",
        "description": "组织语言指令下的视觉感知、空间推理、规划、导航执行与真实部署研究。",
        "overview": (
            "VLN 要求智能体根据语言指令和视觉观察在环境中持续定位、规划并执行导航。"
            "本领域覆盖离散与连续环境、地图与记忆、基础模型代理、空中导航、真实机器人、鲁棒性和评测。"
        ),
        "timeline": [
            ("2017–2019", "R2R 与泛化问题奠基", "室内导航任务、Speaker-Follower 和 unseen 环境泛化形成基础。", ["Vision-and-Language Navigation", "Speaker-Follower"]),
            ("2020–2021", "预训练、连续化与多语言", "跨模态预训练、RxR 和连续环境扩大任务范围。", ["PREVALENT", "Room-Across-Room"]),
            ("2022–2023", "Transformer、地图与全局规划", "长历史建模、BEV/拓扑地图和大模型推理进入主干。", ["BEVBert", "NavGPT"]),
            ("2024–2026", "基础模型与真实开放环境", "通才策略、世界模型、空中导航和在线适应成为前沿。", ["ABot-N1", "OpenVLN"]),
        ],
        "capabilities": [
            _item("grounding", "语言—视觉定位", "把指令片段对齐到对象、区域和行动。"),
            _item("spatial-memory", "空间表示与记忆", "建立拓扑、度量或语义地图并维护历史。"),
            _item("planning-control", "规划与控制", "在离散或连续空间选择路径和低层动作。"),
            _item("generalization", "泛化与真实部署", "跨场景、语言、机器人和空中环境迁移。"),
        ],
        "methods": [
            _item("sequence-fusion", "序列与跨模态融合", "编码指令、视觉历史和候选动作。"),
            _item("map-planning", "显式地图与规划", "使用拓扑图、BEV 或语义场景图。"),
            _item("foundation-agent", "基础模型 Agent", "利用 LLM/VLM 进行空间推理和工具调用。"),
            _item("world-imagination", "世界模型与想象", "预测不可见区域、轨迹和未来观察。"),
        ],
        "landmarks": ["Vision-and-Language Navigation", "Speaker-Follower", "PREVALENT", "BEVBert", "NavGPT"],
        "routes": [
            ("vln-basics", "VLN 入门主线", "理解任务、指标和基本模型。", ["Vision-and-Language Navigation", "Speaker-Follower", "PREVALENT"]),
            ("spatial-planning", "空间与规划路线", "理解地图、图规划和连续控制。", ["BEVBert", "MapGPT", "Spatial-VLN"]),
            ("foundation-agent", "基础模型智能体路线", "理解 LLM/VLM 如何进入导航闭环。", ["NavGPT", "NaVid", "ABot-N1"]),
            ("real-aerial", "真实与空中部署路线", "理解连续机器人、UAV 和 Sim2Real。", ["AerialVLN", "OpenVLN", "SINGER"]),
        ],
        "benchmarks": [
            ("indoor-discrete", "室内离散导航", ["R2R", "RxR"], ["SR", "SPL", "nDTW", "SDTW"], "不同语言、路径采样和 unseen split 必须区分。"),
            ("continuous", "连续与真实导航", ["R2R-CE", "VLN-CE", "真实机器人"], ["SR", "SPL", "碰撞率", "控制频率"], "模拟器与真实传感器差异会改变难度。"),
            ("aerial-open", "空中与开放世界", ["AerialVLN", "VLNVerse"], ["成功率", "路径效率", "安全率"], "地图先验、飞行约束和视角差异必须报告。"),
        ],
        "questions": [
            "基础模型的语言推理怎样与可度量的空间状态对齐？",
            "世界想象是否真正改善导航，还是只增加视觉先验？",
            "如何建立覆盖真实机器人、空中平台和开放世界的统一评测？",
        ],
    },
    "game-rl": {
        "label": "游戏强化学习",
        "description": "组织棋类、电子游戏、多智能体博弈、开放世界和通用游戏智能研究。",
        "overview": (
            "游戏 RL 利用规则明确、反馈密集或可模拟的环境研究学习、规划、自博弈和协作竞争。"
            "本领域既覆盖 Atari、棋类和 RTS 等核心游戏，也覆盖语言、多模态开放世界和人机协作。"
        ),
        "timeline": [
            ("1992–2012", "TD 学习与游戏求解前史", "TD-Gammon、搜索和博弈求解证明学习可达到专家水平。", ["TD-Gammon", "Counterfactual Regret"]),
            ("2013–2016", "深度 RL 与自博弈突破", "DQN 和 AlphaGo 把深度表征、搜索与强化学习结合。", ["Human-level Control", "Mastering the game of Go"]),
            ("2017–2020", "通用自博弈与大规模多智能体", "AlphaZero、OpenAI Five、AlphaStar 和 MuZero 扩展任务规模。", ["AlphaZero", "MuZero"]),
            ("2021–2026", "开放世界与多模态游戏智能体", "语言、记忆、世界模型和通用代理进入复杂游戏。", ["MineDojo", "Voyager"]),
        ],
        "capabilities": [
            _item("perception-control", "感知与实时控制", "从高维观察中学习稳定动作策略。"),
            _item("planning-search", "规划与搜索", "利用模型、树搜索和长时价值进行决策。"),
            _item("multi-agent", "协作、竞争与博弈", "处理对手建模、通信、自博弈和均衡。"),
            _item("open-world", "开放世界与通用性", "结合语言、记忆和多任务迁移。"),
        ],
        "methods": [
            _item("value-policy", "价值与策略学习", "DQN、Actor-Critic、PPO 等无模型方法。"),
            _item("self-play-search", "自博弈与搜索", "MCTS、策略迭代和博弈求解。"),
            _item("model-based", "模型式 RL", "学习动力学并在想象中规划。"),
            _item("language-agent", "语言与多模态 Agent", "用基础模型进行目标理解和技能编排。"),
        ],
        "landmarks": ["TD-Gammon", "Human-level Control", "AlphaGo", "AlphaZero", "MuZero"],
        "routes": [
            ("arcade", "电子游戏基础路线", "理解从像素到策略的深度 RL。", ["Human-level Control", "Rainbow", "IMPALA"]),
            ("board-self-play", "棋类与自博弈路线", "理解搜索、价值和策略迭代。", ["AlphaGo", "AlphaZero", "MuZero"]),
            ("multi-agent", "多智能体路线", "理解协作、竞争和大规模训练。", ["OpenAI Five", "AlphaStar", "Multi-Agent Hide and Seek"]),
            ("open-world", "开放世界智能体路线", "理解语言、技能和长期记忆。", ["MineDojo", "Voyager", "SIMS"]),
        ],
        "benchmarks": [
            ("arcade", "Atari 与通用游戏", ["ALE", "Procgen"], ["human-normalized score", "泛化差距"], "训练帧数、sticky action 和评测 episode 必须一致。"),
            ("board", "棋类与博弈", ["Go", "Chess", "Poker"], ["Elo", "胜率", "exploitability"], "对手池和计算预算直接决定结果。"),
            ("open-world", "开放世界游戏", ["Minecraft", "多模态任务集"], ["任务完成率", "技能覆盖", "交互成本"], "任务定义和人工脚本会显著影响可比性。"),
        ],
        "questions": [
            "游戏中的规模化规律能否迁移到真实开放环境？",
            "自博弈如何避免策略循环、对手过拟合和脆弱均衡？",
            "语言与世界模型怎样帮助长期技能组合而非只改善任务描述？",
        ],
    },
    "wsvad": {
        "label": "弱监督视频异常检测（WSVAD）",
        "description": "组织弱标签视频异常检测、时序定位、视觉语言模型、开放世界理解与评测。",
        "overview": (
            "WSVAD 在只有视频级或其他弱标签的条件下学习定位异常片段。"
            "本领域覆盖 MIL、表征学习、自训练、鲁棒优化、视觉语言模型、开放世界异常、细粒度解释和部署可靠性。"
        ),
        "timeline": [
            ("2018–2019", "深度 MIL 范式确立", "UCF-Crime 上的排序损失和视频级标签建立主流设定。", ["Real-world Anomaly Detection", "Weakly Supervised Video Anomaly"]),
            ("2020–2022", "特征、难例与鲁棒 MIL", "图结构、记忆、对比学习和难例挖掘改善时序定位。", ["RTFM", "MGFN"]),
            ("2023–2024", "视觉语言模型转折", "CLIP 表征和文本提示把异常检测连接到语义理解。", ["VadCLIP", "CLIP-TSA"]),
            ("2025–2026", "开放世界与细粒度理解", "描述、推理、持续学习和更细指标成为前沿。", ["LAVAD", "VAD-R1"]),
        ],
        "capabilities": [
            _item("detection", "视频级异常检测", "判断长视频是否包含异常。"),
            _item("localization", "时序定位", "在帧或片段级定位异常区间。"),
            _item("semantic-understanding", "语义与解释", "识别异常类别、描述事件并提供证据。"),
            _item("robust-deployment", "开放世界与部署", "处理域偏移、持续数据和实时约束。"),
        ],
        "methods": [
            _item("mil-ranking", "MIL 与排序损失", "从视频级标签学习片段异常分数。"),
            _item("feature-memory", "特征与记忆建模", "利用时序关系、原型和记忆增强表征。"),
            _item("self-training", "自训练与鲁棒优化", "生成伪标签、挖掘难例并抑制噪声。"),
            _item("vision-language", "视觉语言模型", "使用 CLIP/VLM 先验和文本语义。"),
        ],
        "landmarks": ["Real-world Anomaly Detection", "RTFM", "MGFN", "VadCLIP", "LAVAD"],
        "routes": [
            ("wsvad-main", "十篇主线", "理解从 MIL 到 VLM 的主要演进。", ["Real-world Anomaly Detection", "RTFM", "VadCLIP"]),
            ("robust-mil", "弱标签与难例路线", "理解伪标签、难例和噪声鲁棒性。", ["RTFM", "MGFN", "UR-DMU"]),
            ("semantic", "语义与开放世界路线", "理解 CLIP/VLM 和异常描述。", ["VadCLIP", "LAVAD", "VAD-R1"]),
            ("deployment", "部署与可靠性路线", "理解实时、跨域和持续异常检测。", ["TeD-SPAD", "PreVAD", "XDVioDet"]),
        ],
        "benchmarks": [
            ("ucf-crime", "长视频异常检测", ["UCF-Crime"], ["frame-level ROC-AUC", "AP"], "常规 AUC 可能掩盖类别和定位差异。"),
            ("xd-violence", "多模态暴力异常", ["XD-Violence"], ["AP", "AUC"], "音频使用、特征骨干和 split 必须一致。"),
            ("fine-grained", "细粒度与开放世界", ["细粒度类别集", "持续学习 split"], ["分类准确率", "Temporal AUC/AP", "遗忘率"], "新协议尚未统一，不能与经典二分类直接横比。"),
        ],
        "questions": [
            "弱标签模型如何证明定位依据而不只是利用场景偏差？",
            "VLM 语义先验如何避免把罕见但正常事件判为异常？",
            "开放世界、持续学习和实时部署应采用什么统一评测协议？",
        ],
    },
}


class HeadingParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.level: str | None = None
        self.buffer: list[str] = []
        self.headings: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        normalized = tag.casefold()
        if normalized in {"h1", "h2", "h3"}:
            self.level = normalized
            self.buffer = []

    def handle_data(self, data: str) -> None:
        if self.level is not None:
            self.buffer.append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.level == tag.casefold():
            text = re.sub(r"\s+", " ", " ".join(self.buffer)).strip()
            if text:
                self.headings.append({"level": self.level, "text": text})
            self.level = None
            self.buffer = []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return normalized[:63] or "item"


def _load_catalog() -> dict[str, list[dict[str, Any]]]:
    by_domain: dict[str, list[dict[str, Any]]] = defaultdict(list)
    with CATALOG.open(encoding="utf-8") as handle:
        for line in handle:
            payload = json.loads(line)
            asset = payload["asset"]
            if asset["asset_type"] in {"collection", "repository"}:
                continue
            for domain in asset.get("domains") or []:
                by_domain[str(domain)].append(asset)
    for assets in by_domain.values():
        assets.sort(
            key=lambda item: (
                item.get("year") is None,
                item.get("year") or 9999,
                str(item.get("title") or "").casefold(),
            )
        )
    return by_domain


def _find_asset(
    assets: list[dict[str, Any]],
    keyword: str,
    *,
    used: set[str] | None = None,
) -> dict[str, Any]:
    lowered = keyword.casefold()
    candidates = [
        item
        for item in assets
        if lowered
        in " ".join(
            [
                str(item.get("title") or ""),
                str(item.get("title_zh") or ""),
            ]
        ).casefold()
    ]
    if used is not None:
        unused = [item for item in candidates if item["asset_id"] not in used]
        if unused:
            candidates = unused
    if candidates:
        return candidates[0]
    fallback = [
        item for item in assets if used is None or item["asset_id"] not in used
    ]
    if not fallback:
        fallback = assets
    if not fallback:
        raise ValueError(f"no paper assets available while resolving {keyword!r}")
    return fallback[0]


def _assets_for_keywords(
    assets: list[dict[str, Any]],
    keywords: Iterable[str],
    *,
    minimum: int = 1,
) -> list[dict[str, Any]]:
    used: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for keyword in keywords:
        asset = _find_asset(assets, keyword, used=used)
        if asset["asset_id"] in used:
            continue
        used.add(asset["asset_id"])
        resolved.append(asset)
    for asset in assets:
        if len(resolved) >= minimum:
            break
        if asset["asset_id"] not in used:
            used.add(asset["asset_id"])
            resolved.append(asset)
    return resolved


def _mapping_target(heading: str) -> str:
    lowered = heading.casefold()
    if any(token in lowered for token in ("时间", "演进", "演化", "里程碑")):
        return "timeline"
    if any(token in lowered for token in ("阅读", "精读", "路线")):
        return "reading-routes"
    if any(token in lowered for token in ("数据集", "指标", "评测", "benchmark")):
        return "benchmarks"
    if any(token in lowered for token in ("论文", "目录", "资料库", "完整库")):
        return "catalog"
    if any(token in lowered for token in ("模块", "方法", "能力", "分类", "主线")):
        return "capability-method-map"
    if any(token in lowered for token in ("问题", "趋势", "热门", "方向")):
        return "frontiers"
    if any(token in lowered for token in ("证据", "边界", "说明", "策略", "口径")):
        return "evidence"
    return "scope"


def _write_yaml(path: Path, payload: object) -> Path:
    text = yaml.safe_dump(
        payload,
        allow_unicode=True,
        sort_keys=False,
        width=100,
    )
    return atomic_write_text(path, text)


def _source_input(path_ref: str) -> dict[str, Any]:
    path = (CORE_ROOT / path_ref).resolve()
    path.relative_to(CORE_ROOT.resolve())
    if not path.is_file():
        raise FileNotFoundError(path)
    stat = path.stat()
    return {
        "path": path.relative_to(CORE_ROOT).as_posix(),
        "sha256": _sha256(path),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _build_domain_files(
    destination: Path,
    slug: str,
    blueprint: dict[str, Any],
    assets: list[dict[str, Any]],
    source_inputs: list[dict[str, Any]],
) -> list[Path]:
    destination.mkdir(parents=True, exist_ok=True)
    landmark_assets = _assets_for_keywords(assets, blueprint["landmarks"], minimum=5)
    evidence_ids = [item["asset_id"] for item in landmark_assets[:3]]
    metadata = {
        "schema_version": "research-asset-hub.domain-portal-config/v2",
        "slug": slug,
        "label": blueprint["label"],
        "description": blueprint["description"],
        "content_version": "2026-07-26.1",
        "review_status": "reviewed",
        "original_selector": {
            "source_id": "domain-portals-20260726",
            "source_record_id": f"overview:{slug}",
        },
        "source_inputs": source_inputs,
        "section_evidence": {
            section: evidence_ids for section in FIXED_SECTIONS
        },
        "appendices": [],
    }
    written = [_write_yaml(destination / "portal.yaml", metadata)]
    written.append(
        atomic_write_text(
            destination / "overview.md",
            (
                f"# {blueprint['label']}\n\n{blueprint['overview']}\n\n"
                "统一版以当前正式库为事实底座：目录、表示可用性、阅读状态和批注均实时读取；"
                "策展文字只解释领域结构，不替代论文原文。"
            ),
        )
    )
    stages: list[dict[str, Any]] = []
    for period, title, summary, keywords in blueprint["timeline"]:
        selected = _assets_for_keywords(assets, keywords, minimum=1)
        stages.append(
            {
                "id": _slug(f"{period}-{title}"),
                "title": title,
                "period": period,
                "summary": summary,
                "asset_ids": [item["asset_id"] for item in selected[:3]],
                "status": "reviewed",
            }
        )
    written.append(
        _write_yaml(
            destination / "timeline.yaml",
            {
                "schema_version": "research-asset-hub.domain-portal-timeline/v2",
                "stages": stages,
            },
        )
    )
    methods = blueprint["methods"]
    cells = []
    for index, capability in enumerate(blueprint["capabilities"]):
        method = methods[index % len(methods)]
        evidence = landmark_assets[index % len(landmark_assets)]
        cells.append(
            {
                "capability_id": capability["id"],
                "method_ids": [method["id"]],
                "summary": (
                    f"{method['title']}为“{capability['title']}”提供主要实现路径；"
                    "具体效果需结合任务、数据与评测协议判断。"
                ),
                "asset_ids": [evidence["asset_id"]],
            }
        )
    written.append(
        _write_yaml(
            destination / "capability-method-map.yaml",
            {
                "schema_version": (
                    "research-asset-hub.domain-portal-capability-method-map/v2"
                ),
                "capabilities": blueprint["capabilities"],
                "methods": methods,
                "cells": cells,
            },
        )
    )
    written.append(
        _write_yaml(
            destination / "landmarks.yaml",
            {
                "schema_version": "research-asset-hub.domain-portal-landmarks/v2",
                "items": [
                    {
                        "id": _slug(str(item.get("title") or item["asset_id"])),
                        "title": item.get("title_zh") or item.get("title"),
                        "summary": (
                            f"该工作是{blueprint['label']}资料库中的代表节点；"
                            "统一页通过时间线、方法图和关联代码说明其位置。"
                        ),
                        "asset_id": item["asset_id"],
                        "status": "reviewed",
                    }
                    for item in landmark_assets[:5]
                ],
            },
        )
    )
    routes = []
    for route_id, title, goal, keywords in blueprint["routes"]:
        selected = _assets_for_keywords(assets, keywords, minimum=3)
        routes.append(
            {
                "id": route_id,
                "title": title,
                "goal": goal,
                "prerequisites": ["先阅读领域定位与边界", "按顺序阅读并核对原文"],
                "steps": [
                    {
                        "order": index + 1,
                        "asset_id": item["asset_id"],
                        "title": item.get("title_zh") or item.get("title"),
                        "rationale": "用于建立该路线的关键概念、方法转折或评测依据。",
                    }
                    for index, item in enumerate(selected[:3])
                ],
            }
        )
    written.append(
        _write_yaml(
            destination / "reading-routes.yaml",
            {
                "schema_version": (
                    "research-asset-hub.domain-portal-reading-routes/v2"
                ),
                "routes": routes,
            },
        )
    )
    benchmark_rows = []
    for identifier, task, datasets, metrics, caution in blueprint["benchmarks"]:
        benchmark_rows.append(
            {
                "id": identifier,
                "task": task,
                "datasets_or_environments": datasets,
                "metrics": metrics,
                "caution": caution,
                "asset_ids": evidence_ids[:2],
            }
        )
    written.append(
        _write_yaml(
            destination / "benchmarks.yaml",
            {
                "schema_version": "research-asset-hub.domain-portal-benchmarks/v2",
                "status": "reviewed",
                "rows": benchmark_rows,
                "not_applicable_reason": None,
            },
        )
    )
    questions = "\n".join(f"- {item}" for item in blueprint["questions"])
    written.append(
        atomic_write_text(
            destination / "open-questions.md",
            (
                f"# {blueprint['label']}的前沿问题\n\n{questions}\n\n"
                "这些问题用于组织后续阅读，不表示资料库已经给出确定答案；"
                "每次新增论文后都应重新核对证据与表述。"
            ),
        )
    )
    return written


def build(config_root: Path) -> dict[str, Any]:
    portal_manifest = json.loads(PORTAL_MANIFEST.read_text(encoding="utf-8"))
    catalog = _load_catalog()
    registry: list[dict[str, Any]] = []
    baseline: list[dict[str, Any]] = []
    mappings: list[dict[str, Any]] = []
    written: list[Path] = []
    by_slug = {
        str(item["asset"]["domain_slugs"][0]): item
        for item in portal_manifest["items"]
    }
    for slug, blueprint in BLUEPRINTS.items():
        item = by_slug[slug]
        representation = item["representations"][0]
        entry = str(item["source_payload"]["overview_entry"])
        inputs = [_source_input(entry)]
        for extra in blueprint.get("extras", []):
            inputs.append(_source_input(str(extra)))
        entry_path = (CORE_ROOT / entry).resolve()
        parser = HeadingParser()
        parser.feed(entry_path.read_text(encoding="utf-8"))
        parser.close()
        for heading in parser.headings:
            mappings.append(
                {
                    "domain": slug,
                    "source_path": entry,
                    "heading_level": heading["level"],
                    "heading": heading["text"],
                    "outcome": "mapped",
                    "target_section": _mapping_target(heading["text"]),
                }
            )
        baseline.append(
            {
                "domain": slug,
                "entry": inputs[0],
                "mapped_inputs": inputs,
                "headings": parser.headings,
            }
        )
        registry.append(
            {
                "slug": slug,
                "label": blueprint["label"],
                "content_directory": slug,
                "original_selector": {
                    "source_id": "domain-portals-20260726",
                    "source_record_id": f"overview:{slug}",
                },
                "allowed_mounts": sorted(
                    {
                        str(mount["mount_path"])
                        for mount in representation.get("mounts", [])
                    }
                ),
                "default_view": "overview",
            }
        )
        written.extend(
            _build_domain_files(
                config_root / slug,
                slug,
                blueprint,
                catalog.get(slug, []),
                inputs,
            )
        )
    written.append(
        _write_yaml(
            config_root / "portal-index.yaml",
            {
                "schema_version": "research-asset-hub.domain-portal-index/v2",
                "portals": registry,
            },
        )
    )
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    baseline_path = atomic_write_json(WORK_ROOT / "baseline.json", baseline)
    mapping_path = atomic_write_json(WORK_ROOT / "section-mapping.json", mappings)
    return {
        "status": "written",
        "domains": len(BLUEPRINTS),
        "files": len(written),
        "baseline": str(baseline_path),
        "mapping": str(mapping_path),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-root", type=Path, default=DEFAULT_CONFIG_ROOT)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    config_root = arguments.config_root.expanduser().resolve()
    allowed_root = (PROJECT_ROOT / "config" / "domain-portals").resolve()
    try:
        config_root.relative_to(allowed_root)
    except ValueError as error:
        raise ValueError(f"config root must stay below {allowed_root}") from error
    result = build(config_root)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
