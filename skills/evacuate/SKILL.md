---
name: evacuate
description: >
  Get everything off a machine before it is released or destroyed, prove it arrived intact, and
  store it as an ARA-shaped artifact — input (code + config) and output (weights, metrics,
  conclusions, the ablation graph). Trigger BEFORE any release or destroy: "这台可以关了",
  "释放机器", "跑完了把东西拉回来", "存到 S3", "销毁前要留什么", "训练结果拉全了吗",
  "artifact 拉回来了没有", "上次那台机器上的东西还在吗". Also trigger when a pull looks
  finished but nobody checked — a half-transferred checkpoint has a plausible name and passes
  every `exists()` check. Not for pulling DATA in (that is /data-collect) and not for deleting
  data against evidence (that is /data-retire). Releasing the machine itself stays /lease's;
  this is what makes that release safe.
---

# /evacuate — 搬空一台即将消失的机器

**这是 MLClaw 里唯一一个「什么都不做」本身就是破坏性动作的地方。** 别处一条记录写错了以后能改;
这里机器一释放,盘跟着走,没拉走的东西就没了 —— 日志里没有 `rm`,没有任何东西 raise。

已经发生过好几次的形态是同一个:训练跑完,checkpoint 拉了一半,没人看,机器释放。
留下的是一个名字很正常、**打不开**的 `.pth`,和一张缺了尾巴的指标表。
`os.path.exists` 从头到尾都说「在」。

而本来能拦住的两样东西,都在看别处:

- `lease.py release` 只验**机器**没了(`verified_gone`,计费那一侧);
- `pool.py release --artifacts recovered` 直接**采信操作者的话**。

## ‼️ 这个 skill 建立在一句话上

**把文件留在一台即将销毁的机器上,和删掉它是同一个动作。**

所以 CLAUDE.md 那条「Never delete a checkpoint outside `retention.py plan` → `apply`……
Never delete a file you cannot rank」在这里**同样成立**。`plan` 会拒绝把一个没被排过序的
checkpoint 留下 —— 同一条中止条件,只不过执行删除的是「机器消失」而不是 `rm`。

想扔?拿 `--retention-plan <path>` 来,让**当初做出那个排序的东西**在场。一串文件名不算证据,
在这儿和在那儿一样不算。

## 存成什么形状:一份 ARA

工件是 **`/ara`** 的,这里只是调它 —— `bundle` 那个动词就是。五层(`src` / `evidence` /
`logic` / `trace` / `weights`,最后一层是 ARA 没有的)、`ARTIFACT.md`、可复现判定,
全在那边,不在这里。

**为什么不是这个 skill 的:** 一次撤离的作用域是**一台机器** —— 上面可能有三轮的碎片、
也可能一份工件都没有,还有一堆**不属于任何工件**的文件(`unclassified` 那个桶就是证据:
`/ara` 没理由带上它们,而这里必须带,因为机器要没了)。而且它被**租约**闸住。
工件的作用域是**一轮**,没有截止时刻。

成立的是另一句:**机器消失前的那一刻,是源头最后一次可读。** 所以这个截止时刻**逼**工件
必须完成 —— 是调用,不是包含。分层的判定函数从 `/ara` **导入**,不在这里重写:
两个分类器会让同一个 checkpoint 在这边进 `weights/`、在那边进 `src/`,而只有工件会显出来。

这个 skill 独有、`/ara` 无从知道的那一样,是 **`ARTIFACT.md` 里的 Transfer 段**:
它指名的那些字节到底有没有到。

## 顺序不能换

脚本 `<mlclaw_root>/scripts/evacuate/evacuate.py`，七个动词：

```
plan → freeze → push → verify → bundle → clearance      (+ status)
```

**`freeze` 必须在 `push` 之前,这条是全部保证所在。** 传完再列清单,列的是「到了什么」——
那是同义反复,按构造对**每一次**半截传输都判通过。清单在**源头**冻结,完整性拿它算。
`push` 走在 `freeze` 前面会被拒。

‼️ **机器已经没了才想起来冻结,答案是 `unverifiable`,而且是永久的。**
到了的东西无法为没到的东西作证。

## 四种到达状态,`exists()` 一种都分不出来

| | 什么意思 | 谁能看见 |
|---|---|---|
| `verified` | 哈希对上了 | 哈希 |
| `size_only` | 在,长度对,**没比过哈希** | 长度。截断能抓,损坏抓不到 |
| `truncated` | 在,**长度不对** ← **就是「拉一半」** | 长度 |
| `corrupt` | 在,长度对,**字节不对** | 只有哈希 |
| `missing` | 不在 | —— |
| `unverifiable` | **目的地没应答** | 什么都没看见,**绝不能写成 `missing`** |

`size_only` 不是懒:S3 的 ETag 只在单段上传时等于 MD5,所以**恰恰是大 checkpoint**
会拿到一个永远比不了的哈希。上传时带 `--checksum-algorithm SHA256`,`head-object` 才会回
`ChecksumSHA256`。回不了就是 `size_only` —— 它**不是** `verified`。

## 放行:三种结论,算出来的,不是声明的

| verdict | 什么时候 | 谁读它 |
|---|---|---|
| `clear` | 每个文件哈希验过 | `lease.py release` / `pool.py release` |
| `clear_size_only` | 都在、长度都对,N 个没比过哈希。**准放行,并且把缺口说出来** | 同上 |
| `blocked` | 任何 missing / truncated / corrupt / unverifiable / 未排序 / **被引用却没搬走** | 退 1 |

没有第四种叫「应该没事」。**`clearance` 退 1 的时候不要释放。**

### 被引用却没搬走 —— 这条 join 没有别的东西在做

一条结论引用了 `stages/training/runs/run_X`。机器一销毁,**引用不会断** ——
`conclusions.json` 里它照样解析得通,那条结论照样读起来成立,几周后才悄悄变成
`unverifiable`。这就是 CLAUDE.md「Never delete data a frozen snapshot still names」
在模型这一侧的样子,**而模型这侧没有 `retire.py` 知道这件事**。

三种情况算安全:本地项目里已经有了(最常见,别在这上面误报)、源根就是它、清单覆盖了它。
其余的报出来,让人去看。

## 可复现是**读**出来的,不是声称的

`code_snapshot.py` 在启动时就算过 `code.reproducible` 了,这里只**读**。
`false` 的意思很具体:有个改动过的文件太大没能嵌进去,所以 `git checkout && git apply`
重建出来的是**另一棵树**。

‼️ **这一条不拦。** 丢字节比标签不准严重得多,所以东西照搬,但那行判语会印在
`ARTIFACT.md` 第一屏 —— 和普查记 `complete: false` 而不是干脆不给,是同一条规矩。

一个 `src` 层是空的 bundle 会被点名:那是**备份**,不是工件,而且从它身上读不出
一次 ablation 里两条臂到底差在哪。

## 三条不做的事

- **不释放机器。** 那是 `/lease` 的。这个 skill 只负责让那次释放安全。
- **不搬字节。** `aws s3` 干这个,和 `/data-collect` 一样的分工:这里决定什么必须走、
  冻结那是什么、以及裁定到了什么。
- **不删源头。** 搬走 ≠ 清空。删除要走 `/data-retire` 或 `retention.py`。
