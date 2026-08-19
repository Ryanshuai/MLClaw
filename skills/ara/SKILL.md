---
name: ara
description: >
  Assemble a round's work into an Agent-Native Research Artifact — the five layers, an
  ARTIFACT.md a person can read a year later, and a check that says whether the frozen copy
  still agrees with the live record. Trigger at the end of an exploration or a tune session,
  after /conclude, and whenever somebody says "把这轮的东西整理成一份", "出一份工件",
  "交接给别人", "这轮的产出在哪", "ARA", "整理成可复现的一份", "归档这一轮". Also trigger
  before citing an old artifact, because its statuses and tiers were frozen at build time and
  do not update themselves. Not for getting bytes off a machine before it is destroyed
  (that is /evacuate, which calls this) and not for writing the conclusions themselves
  (that is /conclude, which fills the logic layer).
---

# /ara — 一轮的产出，做成一份能读的工件

ARA(arXiv:2604.24658)的主张是:**工件本身就是研究对象**,不是研究的副产品。它给了四层:
`logic/`(主张什么)、`src/`(什么产生了它)、`evidence/`(数)、`trace/`(探索 DAG)。

MLClaw 四层全都在产,只是**没有地方放**。一轮跑完剩下的是一堆 run 目录 ——
那和「一份别人能读的工件」是两回事。

## 五层

| 层 | 装什么 |
|---|---|
| `src/` | code snapshot、`config_snapshot.json`、`sources.json`、env。**搞架构搜索时代码就是自变量**,所以这层不是背景,**它就是可复现性本身** |
| `evidence/` | `stream.jsonl`、指标、`tb/`、**原始日志**。日志在这层是因为 MLClaw 的接地规矩要求数字引到抄下来的那一行——**那一行就是 evidence** |
| `logic/` | `knowledge/conclusions.json` —— `/conclude` 的产出 |
| `trace/` | `graph.json`、`findings.json`、`baseline.json` —— `/explore` 的实验图。**这层决定一年后那次 ablation 还读不读得懂** |
| **`weights/`** | **‼️ ARA 没有这层。** 不是疏漏:论文的工件是**知识**,知识能从 `src+evidence` 重新长出来,**4GB 的 checkpoint 不能**。它是唯一不可重建的一层 |

外加一个**不是层**的桶:`unclassified` —— 规则没认出来的,**照留不误并且点名**。
只留认得出的东西的清扫,正是「谁都没想到的那个文件」丢掉的方式,而且它一边丢一边报成功。

## 两个动词

```
<mlclaw_root>/scripts/ara/ara.py build --project <P> [--root <R>] [--out <D>]
<mlclaw_root>/scripts/ara/ara.py check --project <P>
```

`build` 默认从**项目**分层,`--root` 换成别的树(`/evacuate` 传的就是那台快没的机器的路径)。

‼️ **每次 `build` 出一份带日期的新工件,不覆盖旧的。** 一份工件是一次**带日期的读数**,
和普查、撤离记录同类。第二轮盖掉第一轮,毁掉的是「第一轮当时相信什么」的唯一记录 ——
而那正是让第一轮那批 run 读得懂的东西。要原地重建得显式 `--id <既有>`。
`logic/` 和 `trace/` 会**实体拷进工件**——它们必须在不下载权重的前提下可读,
而且权重没了的时候活下来的正是它们。

## ‼️ `check` 抓的是「冻结的信念不会自己更新」

这是 CLAUDE.md 那条「Never repeat a conclusion without re-reading its status」
**上升一层**,发生在真正被人读的那个副本里。

`/conclude` 的 `status` 和 `tier` 是**算出来**的;工件把它们**冻住**。证据烂掉的时候,
冻结的那份**一个字都不会变**——而交接出去的就是那一份。

```
K01: the artifact froze `supported` and the record now says `unverifiable`.
     The frozen copy is the one people read, and nothing about it changed
     when its evidence did
```

**报告,不修。** 修了就把「工件比它的证据活得长」这个唯一证据抹掉,同时把报告变绿 ——
和 `graph.py check`、`conclude.py check` 同一条规矩。

引用一份旧工件之前先跑 `check`。

## 可复现是读出来的

`code_snapshot.py` 启动时就算过 `code.reproducible`,这里只读,不重新推导 ——
它已经拒掉了它该拒的,这里再来一个「第二意见」只可能和它打架。

`false` 的意思很具体:有个改动过的文件太大没嵌进去,`git checkout && git apply`
重建出来的是**另一棵树**。

‼️ **不拦。** 丢字节比标签不准严重得多,判语印在第一屏就够了 ——
和普查记 `complete: false` 而不是干脆不给,同一条规矩。

**没有 `src/` 层的工件会被点名:那是备份,不是工件**,而且从它身上读不出一次 ablation
里两条臂到底差在哪。没有 `logic/` 层会被指去 `/conclude`。

## 和 `/evacuate` 的关系:是被调用,不是被包含

一次撤离的作用域是**一台机器** —— 上面可能有三轮的碎片、也可能一份工件都没有,
还有一堆不属于任何工件的文件,而且它被**租约**闸住。这个 skill 的作用域是**一轮**,
**没有截止时刻**。

真正成立的是:**机器消失前的那一刻,是源头最后一次可读**。所以那个截止时刻**逼**工件必须
完成,而不是包含它。

> 同构的例子:`/train-run` 在微调时调 `/eval-run` 去测基线。那不代表 train-run 是 eval
> 的一个 stage,只代表**那是唯一还能测的时刻**。

## 以后每一轮都要出

不是靠正文里写一句,有两个地方在拦:

- **`graph.py check`** —— 一轮里有 settled 的卡、却没有工件(或者工件**早于**最后一次结算,
  那它描述的是另一轮),报 **major**。是 major 不是 critical:少一张封面不该拦住下一条臂,
  CLAUDE.md 把拒绝留给「会让下一次测量出错」的事。
- **`evacuate.py clearance`** —— 没人建就**就地建**。不是「不建就不放行」:为一个 markdown
  文件卡住一台还在烧钱的机器是死锁,而且丢字节比什么都严重。**建它属于「直接做」那一桶** ——
  便宜、本地、可撤销。它唯一不许做的是**失败了不吭声**。

## 三条不做的事

- **不写结论。** 那是 `/conclude` 的,它填 `logic/` 层。
- **不搬字节、不碰机器。** 那是 `/evacuate` 的。
- **不修工件。** `check` 只报。重建就是再跑一次 `build`。
