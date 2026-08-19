---
name: conclude
description: >
  Record what a round CONCLUDED — the belief, its evidence with quotes, what would overturn
  it, and what it rests on — as a checkable artifact rather than a sentence. Trigger at the
  end of an exploration, a tune session, an eval round, or an audit; whenever somebody says
  "所以结论是什么", "这轮学到了什么", "记一下这个结论", "把结论写下来", "以后别再试这个了";
  and whenever a past conclusion is being quoted — "我们试过了没用", "那个不是早就否掉了吗",
  "这个还成立吗" — because the answer depends on a corpus, a tier and a noise floor that the
  sentence does not carry. Also trigger when a dataset is retired or a run deleted, to find
  which conclusions just became unverifiable. Not for recording what HAPPENED (that is the run
  record) or which arm won (that is /explore's graph).
---

# /conclude — 结论层

一个 run 记录是**发生了什么**。一张 graph 卡是**哪条臂赢了**。都没有记**现在相信什么** ——
而六周之后被人复述的，只有后者，而且复述时那三个限定词一个都不剩：

> 「多帧融合我们试过了，没用。」

这句话是关于**某个语料**、**某个档位**、**某个噪声地板**的。三样都不在句子里，所以它既没法被
反驳，也没法被应用——而它会被应用，在一个从来没测过它的语料上。

结构借自 ARA（arXiv:2604.24658）的 `logic/claims.md`。拿了三样：

1. **`Evidence basis` 和 `Interpretation` 是两栏，不是一栏。** 合成一栏，结论读起来就像机制被
   测过了，下一轮于是照着一个没人测过的机制去设计。
2. **`Falsification criteria` 是必填。** 没有反证条件的信念是偏好，`check` 拒收。
3. **结论依赖结论。** 所以否掉一条必须**推动**其它条，而不是只改自己。

没拿它的**词**：MLClaw 里 `claim` 已经是反义词——`/ask-human` 和 `/discover` 用它表示
「有人这么说，但没有东西证实」。用 claim 称呼有证据的那个对象，正好在差别最要命的地方撞车。

**加了一个 ARA 没有的状态：`unverifiable`。** ARA 的状态假设证据待在原地。MLClaw 会退役数据集、
删检查点、丢快照，所以真正会发生的那个状态是「**现在没人能查了**」。它不是弱一点的
`supported`，更不是 `refuted` —— 和 `census.py` 分开 `gone` / `unreachable`、`/repro` 单列一档
是同一条纪律。

## 记录在哪

`{project}/knowledge/conclusions.json`（记录）+ `knowledge/conclusions.md`（渲染的工件）。
项目级，不在 `stages/` 下面：一条结论比产生它的那次 exploration 活得久，而且可能来自 eval、
tune 或 audit。

脚本 `<mlclaw_root>/scripts/conclude/conclude.py`，九个动词：
`new | add | evidence | set | refute | supersede | check | status | render`。

## ‼️ `status` 和 `tier` 是算出来的，不是写的

`set --field status` 会**拒绝**（退 1）。这不是洁癖：一条置信度活得比它的证据长，在 JSON 里
和证据完好的那条**一模一样**，而这正是这个文件存在的唯一理由。

- **`tier` 取证据里最弱的那一档**，不是最强的。一条结论靠一个 T3 臂加一个 T1 探针，它是
  **T1 结论**。取最强就是 CLAUDE.md 里「软数字变硬数字」那条机制，往上一层。
- **`status` 由 `check` 从「现在能不能打开」算出来**，并**报告**存的值和算的值不一致 ——
  报告，不修。修了就把「结论活得比证据长」这个唯一证据抹掉了，同时把报告变绿。

## 流程

一次五步，别拆成五个问题问人。

1. **`add`** —— 一句话说清相信什么，加上 `--falsified-if` 和 `--corpus`。
   反证条件必须**点到一个数或者 scope 里那个指标名**；「如果后来发现不行」是同义反复，
   它比空着更坏，因为它看起来填了，而且任何测量都满足不了它。
2. **`evidence`** —— 每条证据一个 `--ref` 和一句 `--quote`。**quote 是抄下来的那一行，不是概括**：
   光有路径不算接地，抄下来的行才是「这个源当时被打开过」的证据。`check` 会核对 statement 里
   每个数字是否出现在某条 quote 里。
3. **`--interpretation`** —— 在证据之上**论证**的、没测过的那部分。ARA 自己的例子把
   「the authors argue (but do not formally prove)」放在这一栏。别塞回 statement。
4. **`check`** —— 关键 finding 退 1。
5. **`render`** —— 出工件。

## 判决表

| 情况 | 状态 | 谁能改 |
|---|---|---|
| 证据都打得开，依赖也不弱 | `supported` | —— |
| 有争议 / 依赖被否或存疑 | `contested` | 有人去看 |
| 反证条件被一次**记录在案的测量**满足 | `refuted` | `refute --by <打得开的 ref>` |
| 有证据 ref **打不开了** | `unverifiable` | 修证据，或者接受它就是查不了 |
| 被更准的一条取代 | `superseded` | `supersede --by K0N` |

**否掉一条不会删掉靠它的那些**，只会把它们变 `contested`。删了就把「当初为什么launch 那一批
run」的唯一记录抹了；留在 `supported` 则让一条被否的前提继续被引用。`contested` 是「得有人去
看一眼」那一格。

**`refute` 的 `--by` 必须打得开。** 一个打不开的反证是「意见推翻记录」——CLAUDE.md
「Never let somebody's word become a checked fact」正是这条。

## 什么时候会突然多出一堆 `unverifiable`

`/data-retire apply` 之后、删 run 之后、快照没了之后。跑 `conclude.py check` 就知道**哪几条**
刚刚变成没人能查的。引用了带 `data_retired` 戳的快照时，这里**只报，不裁**——
`/repro` 的 `survivors_of_retirement` 已经会做「删除时间 vs 普查时间」那个 join，
同一个判断写两遍只会有一遍被修。

## 被别的 skill 怎么调

- **`/explore` 收尾必调**：一轮架构搜索的产出就是若干条结论，落在 `graph.json` 里的是
  「哪条臂赢了」，不是「所以现在相信什么」。
- `/train-tune-report`、`/eval-report`、`/data-audit` 之后可选。
- **有人引用旧结论时**（「那个不是早否掉了吗」）：先 `status`，别凭记忆答。

## 三条不做的事

- **不执行任何东西。** 结论由已经存在的 run 支撑；这里一个 GPU 都不碰。
- **不替人下结论。** `provenance` 分 `user` / `ai-suggested` / `ai-executed` / `user-revised`，
  且**永不自动升级**——和 `graph.py` 同一条规矩。全是 `ai-suggested` 的一屏结论，是没人要过的
  一屏结论，`check` 会这么说。
- **不修记录。** `check` 只报。
