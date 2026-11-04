# 一人 AI 团队协作手册（solo-ai-team-playbook）

> 一个人同时指挥多个 AI 会话推进同一个项目时，怎么分工、派单、验收，才不会乱。

## 解决什么问题

多开几个 AI 窗口（多个 Claude Code 窗口、再加 Codex 等）之后，常见的乱象：

- 每个窗口都来找你，你成了人肉传话筒；
- 同一件事几个窗口各说一套，状态文件被互相覆盖；
- AI 自测通过就说“完成了”，没人独立核实；
- 一句“可以”被当成合并、推送、发布的授权。

这套方法论的做法是：

**owner 只跟技术负责人（总控）说话；技术负责人定档、拆任务、写任务包、排程，领域判断交给按阶段装载岗位卡的执行者；施工方只认任务包；每件事只有一份状态、每项只有一个写的人；验收按层级各管一层，不可逆动作逐道门禁、明确授权。**

## 定位：治理层，不是执行框架

Spec Kit、BMAD、CCPM 解决“怎么把东西做出来”（规格、拆任务、并行执行），Claude Code 的 agent teams 解决“多个会话怎么一起跑”。本手册解决的是剩下那块：**谁说了算、什么算完成、什么需要 owner 授权**。它可以叠加在这些工具之上用，对应关系见 `PLAYBOOK.md` 第 11 节。

## 适用与不适用

- **适用**：一个 owner + 2 个以上 AI 会话协作推进同一项目；同一种 AI 多开或多种 AI 混用都行；内容型项目、代码项目都行。
- **不适用**：单个窗口就能做完的小事——直接做，不需要这套流程。

## 文件

| 文件 | 内容 |
|---|---|
| [PLAYBOOK.md](PLAYBOOK.md) | 完整方法论：原则（附“不这样会怎样”）、角色、流转、路由、并行、资源压力、文档纪律、与现有工具的配合、岗位卡、规则理由 |
| [examples/walkthrough-medium.md](examples/walkthrough-medium.md) | 端到端示例：一件中档需求从提出到收口，附小档对照 |
| [CLAUDE-snippet.md](CLAUDE-snippet.md) | 十几条的摘要，可直接贴进 `CLAUDE.md` 或 `AGENTS.md` |
| [templates/task-package.md](templates/task-package.md) | 任务包模板（派给施工方的自含指令） |
| [templates/report.md](templates/report.md) | 回报模板 |
| [templates/status.md](templates/status.md) | 项目状态文件模板（含多线总览） |

## 快速开始

0. 先花 5 分钟看 `examples/walkthrough-medium.md`，了解整套流转长什么样。
1. 把 `CLAUDE-snippet.md` 的内容贴进项目根 `CLAUDE.md`（或个人全局 `~/.claude/CLAUDE.md`），把其中“完整版见”的路径改成 `PLAYBOOK.md` 在你机器上的位置。
2. 开一个总控窗口，让它按 `PLAYBOOK.md` 第 10 节搭最小起步：状态文件、任务包和回报的存放位置、本项目版岗位卡。
3. 之后所有需求只对总控说。总控接单时会先回一行“→ 档位／岗位卡／执行者，原因”，你看到不对可以当场叫停。

## 术语

| 术语 | 含义 |
|---|---|
| owner | 项目发起人：提需求、拍板、手测验收、对门禁签字的那个人 |
| 技术负责人（总控） | 唯一对接 owner 的窗口，负责定档、拆任务、写任务包、派单、排程、核证据 |
| 岗位卡 | 一张一屏的职责说明（需求分析师、产品经理、开发工程师、QA）；职能是卡，不是窗口 |
| 执行者 | 装载岗位卡干活的窗口或子代理，一个执行者可先后装载多张卡 |
| 任务包 | 施工方拿到就能做、不必回头查的自含指令文件 |
| 门禁 | 合并、推送、发布、部署等不可逆动作，每道单独授权 |

## 现状与局限

- 提炼自内容型项目和功能型代码项目的真实多窗口协作实践。
- 第二版新增部分（岗位卡、规模分档、阶段路由、资源压力）经过两轮独立复核，但**尚未在真实多线任务中完整试运行**。
- 部分做法依赖工具能力（窗口间互发消息、查看上下文占比和额度），你的环境不一定支持，不支持时按“写文件 + owner 转达”降级。
- 示例以 Claude Code + Codex 为主，换成其他 AI 工具同样适用。

## 参考

- Anthropic：《Building effective agents》《How we built our multi-agent research system》
- Claude Code 文档：best practices、subagents、[agent teams](https://code.claude.com/docs/en/agent-teams)
- MetaGPT（按软件公司岗位分工的多智能体框架）
- [BMAD-METHOD](https://github.com/bmad-code-org/BMAD-METHOD)（按规模分档的 AI 敏捷开发方法）
- [GitHub Spec Kit](https://github.com/github/spec-kit)（规格驱动开发）
- [CCPM](https://github.com/automazeio/ccpm)（用 GitHub Issues 做状态源的多 agent 并行）

## 许可

[CC BY 4.0](LICENSE)：可自由转载、修改、商用，请注明出处。
