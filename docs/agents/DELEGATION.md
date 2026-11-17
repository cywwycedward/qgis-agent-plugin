# 委派与协作流程（agent workflow）

本文件是**可复用的操作规程**：怎么把活派给别的 agent、agent 之间怎么交接、一个功能要经过哪几步。
它不是某次会话的记录——那次会话的产物在 `docs/delegate/`（本机、已 gitignore）。

适用的工具：`hc`（herdr-core，跑终端/进程）+ `herdr-app`（把终端放进人的窗口）。

**本文的「agent」= `hc` 起的终端里的交互式 pi 会话，不是 pi 的 `subagent` 工具。** `subagent` 没有终端 id（`hc send`/`wait`/`read`/`kill` 都寻址不到）、不进人的窗口、与主 agent 共用 cwd，而 §1 的派活、§4 的五步隔离（步骤 2/5 空上下文、1/3/4 同一会话续话）全都依赖这三件事；所以派活一律走 `hc run`，不调 `subagent`。

---

## 0. 三个地址概念

| 概念 | 含义 |
| --- | --- |
| 主 agent | 就是你。跑在主 worktree（本目录），分支 `develop`，终端 `$HC_TERMINAL_ID`（例如 `t13`） |
| 子 agent | 由 `hc run` 起的终端，用 `hc` 收发消息；它们的名字由 `--name` 给 |
| worktree | 每个功能一个，路径 `D:\core\workspace\mine\wt\aof\<name>`，分支 `feature/<name>` |

子 agent 是可观察、可续话、可打断的**交互式** pi 会话。永远不要用 `pi -p`（一次性打印模式）起 agent。

---

## 1. 派活（herdr-core）

```powershell
# 1) 先给它一个独立分支和 worktree
git worktree add D:\core\workspace\mine\wt\aof\f5-qgstask -b feature/qgstask-background develop

# 2) 起 agent：短任务文本 + 细节在文档里
$space = 'sp48a54c28cf08fc9dc3335e0ce9241c2a'      # herdr-app 的 space id，见 §2
hc run --bg --name f5-impl --cwd 'D:\core\workspace\mine\wt\aof\f5-qgstask' `
  --label herdr.space=$space -- `
  pi 'Read docs/delegate/00-charter.md then docs/delegate/F5-task.md inside <主 worktree 绝对路径> and execute F5 completely. When done or blocked run: hc send t13 with a one line report and the path to your handoff doc.'
```

规则（都是踩过的坑）：

- **任务文本短，细节进文档**。argv 里塞长文会遇到引号/换行地狱；把要求写进 `docs/delegate/<F>-task.md`，消息只指向它。
- **起完再发任务**。实测：`hc run ... -- pi '<长文本>'` 只把第一个词交给了 pi（`hc get <t>` 的 `argv` 是
  `["pi", "Read "]`），agent 起来后空等。可靠做法是两条命令：`hc run --bg --name <n> --cwd <wt> -- pi`
  （不带任务），然后 `hc send <n> '<任务>'`；`send` 返回 `started: true` 才算送到。
- **主 agent 不在 `hc` 终端里时没有可寻址的地址**（`$env:HC_TERMINAL_ID` 为空、`hc wait --children`
  报 `needs_target`）。这种情况下让子 agent 把结论写进 handoff 文档、在屏幕上打印 `DONE <路径>`，
  你 `hc wait <t>` 收工再读文件；不要要求它 `hc send` 你。
- **名字用小写**：`[a-z0-9][a-z0-9._-]{0,31}`，形如终端 id（`t7`）的名字会被拒。约定 `<feature>-impl` / `<feature>-review-r<n>` / `<feature>-merge`。
- **只用 ASCII 标点**。消息里出现 `“ ” ；（）` 之类全角符号时，PowerShell/`hc` 的参数解析会被拆开（实测：`unexpected argument`）。中文可以，标点用 `, ; : ( )`。`#` 也会被拆开（`hc send` 实测：`unexpected argument '中文'`），消息里别用它。
- 收消息：`hc send <t> '<一句话>；详见 docs/delegate/X.md'`；需要等它处理完再加 `--wait`。
- 等待：`hc wait <t>`（失活/退出/有消息即返回）、`hc wait <t> --output ready`、`hc wait --children any`。
  **绝不 sleep / 忙轮询**。`reason: timeout` 是例行检查，不是失败，继续等。
- 看进度：`hc read --lines 20 <t>`；`working` 状态只能看屏，停下才有历史。
- 收尾：`hc kill <t>; hc rm <t>`。只清理自己起的终端。

---

## 2. 让它出现在人的窗口里（herdr-app）

```powershell
herdr-app ls                     # 找 space id 与 tab（JSON）
herdr-app status                 # 当前焦点、布局、错误
# 起完 agent 后，它会被自动放进一个新 tab；找到那个 tab 再改名
$ls = herdr-app ls | Out-String
$idx = $ls.IndexOf('"terminal": "' + $addr + '"')
$tab = ([regex]::Matches($ls.Substring(0, $idx), '"id": "(tb[0-9a-f]+)"'))[-1].Groups[1].Value
herdr-app do tab.rename --id $tab --name 'F5 qgstask impl'
```

- `hc run ... --label herdr.space=<id>`（不带 `herdr.tab`）→ 窗口自动开一个新 tab 收编这个终端。
- **tab 名 = 这个 agent 的职责**：`F5 qgstask impl`、`F5 review r2`、`F5 merge`、`A3 audit`、`A3 fix`。
- `herdr-app do` **不要**传 JSON 字符串：PowerShell 5.1 会把内层引号剥掉（`args are not JSON`）。
  用 `--key value` 形式；数组/对象参数才需要 JSON，那就写一个 `.js` 用 `node` 调。
- 窗口只是显示层：关窗不影响终端，进程归 `hc` 管。清理用 `hc kill`，不要用 `herdr-app` 关 tab 来“杀”agent。

---

## 3. agent 之间怎么交流：只通过文档

**位置**：`docs/delegate/`（本机目录，已 gitignore，**不入库**）。
所有 worktree 的 agent 都用**绝对路径**读写主 worktree 的这一份，所以不存在分支冲突，也不需要提交。

**命名**：

| 文件 | 谁写 | 内容 |
| --- | --- | --- |
| `00-charter.md` | 主 agent | 唯一规则书：决策、文件所有权、五步、门禁、汇报格式 |
| `F<n>-task.md` | 主 agent | 这个功能的规格（范围、只许写哪些文件、验收点） |
| `F<n>-implement.md` | 实现 agent | 做了什么、冻结给下游的签名、偏差、未做项 |
| `F<n>-review-r<k>.md` | 评审 agent（第 k 轮） | 问题清单 + 末尾一行 `结论：无问题` / `结论：N 个问题` |
| `F<n>-fix-r<k>.md` | 实现 agent | 逐条回应：改了什么 / 为什么不改 |
| `F<n>-merge.md` | 合并 agent | 合并了什么、冲突、门禁实际输出、矛盾检查、清理结果 |
| `A<n>-audit.md` / `A<n>-fix.md` | 审计/修复 agent | 全局审计与逐条修复 |
| `99-final.md` | 主 agent | 收尾报告 |

**规则**：

- 动手前先读上一份交接文档；长答案写文件，消息只放路径与一句话结论。
- 评审 agent **只写报告**，不改代码；收尾必须 `git checkout -- .`（它的 pi-lens 会自动格式化，否则会被当成实现者的脏工作区）。
- 合并 agent 不改代码逻辑；发现矛盾写报告并上报，不擅自“顺手修”。
- 状态声明必须与事实一致（“工作区干净”要先 `git status` 看过）。

---

## 4. 一个功能的五步

| 步 | 谁 | 上下文 | 在哪个 worktree | 产出 |
| --- | --- | --- | --- | --- |
| 1 调研 + 实现 | 实现 agent | 常驻（此后一直活着） | 该功能的私有 worktree | 代码 + 测试 + `F<n>-implement.md` |
| 2 代码评审 | **空的**新 agent | 从零开始 | 同一个 worktree（只读） | `F<n>-review-r1.md` |
| 3 回应评审 | **同一实现 agent**（原终端） | 保留 | 同一个 worktree | `F<n>-fix-r1.md` |
| 4 循环 2→3 | 每轮新的评审 agent / 同一实现 agent | — | 同上 | 直到 `结论：无问题` |
| 5 合并回主 worktree | **空的**新 agent | 从零开始 | 主 worktree | `F<n>-merge.md` |

约束：

- **同一个功能全程同一个 worktree**，步骤之间不并行。
- 步骤 1/3/4 必须是**同一个 agent 同一个上下文**（它写过这段代码，只有它能解释为什么）。
- 步骤 2 与 5 必须**空上下文新 agent**（避免自我确认）。
- 每一步结束时都必须 `hc send <主 agent> '<结论>；详见 docs/delegate/X.md'`；卡住就 `BLOCKED: <问题>`，不自行扩大范围。
- **只有互不干扰的功能才并行**：文件不重叠、接口不相互依赖。判定就是看“谁写哪些文件”——在 charter 里有一张所有权表。

---

## 5. Git 与 worktree 纪律

- 功能分支从 `develop` 切，命名 `feature/<name>`；纯文档用 `docs/<name>`；缺陷修复用 `fix/<name>`。
- 合并一律 `git merge --no-ff`，保留分支痕迹；然后删分支、注销 worktree。
- `develop` 只靠合并前进：**不要**直接往 develop 提交（主 agent 的机械格式化也要走一个短分支）。
- 不 `git push`；不碰 `main`。
- 顺序陷阱：合并前先 `git branch --show-current`。子 agent 可能把主 worktree 留在功能分支上，于是 `git merge` 变成“自己合自己”（`Already up to date`）。
- worktree 目录常常删不掉：活着的终端把它当 cwd。先 `hc kill` 那些终端，再 `Remove-Item`。

---

## 6. 门禁（每个功能、每次合并都要过）

命令与判据只有一份，在 `DEVELOP.md` §2.3 的检查清单：`npm run check`、裸解释器 `pytest`、
验收跑，三条都绿，按改动类型再加跑。子 agent 报告里贴**实际输出**，不贴“应该能过”。

除此之外，每条修复都要有**“不修就红”的证明**（写进 fix 报告）：改之前该用例红、改之后绿。

---

## 7. 全部功能合并之后的全局审计循环

1. 确认所有 worktree 都已合并回主 worktree、分支已删。
2. 空上下文 agent 跑 `ponytail-audit`（或本仓的审计流程），报告写到 `docs/delegate/A<n>-audit.md`。
3. 另一个空上下文 agent 按报告逐条修复（`A<n>-fix.md`），提交在 `fix/audit-a<n>` 分支；主 agent 机械合并。
4. 重复 2→3，直到审计给出 `结论：无问题`。上一轮的教训：这个循环可能跑十几轮，每一轮都会挖出新的真实形状（安全边界、QGIS 自身行为），不要因为“看着差不多了”就停。

---

## 8. 已知噪声（别当缺陷）

| 现象 | 真相 |
| --- | --- |
| pi-lens 报 `Import "qgis.core"/"pydantic" could not be resolved` | 分析器没指向 QGIS 解释器；测试在 QGIS python 里跑，是绿的 |
| pi-lens 报“文件被脚本改动、告警被扣留” | 子 agent 用脚本改文件导致无法归因；以 `npm run test` 为准 |
| `[stale]` 标记的告警 | 快照过期，重跑或忽略 |
| 验收脚本退出码 1 但 `FAIL 0` | PowerShell 管道的 `$LASTEXITCODE` 假象；单独跑一次确认 `exit=0` |
| `docs/delegate/` 里历史报告提到已删文件 | 历史记录，不改 |

---

## 9. 建议的技能

| 场景 | 技能 |
| --- | --- |
| 派活 / 等结果 / 收尾 | `herdr-core`（`hc`） |
| 让 agent 出现在窗口里 | `herdr-app` |
| 代码评审 | `ponytail-review`（评审 agent 必用） |
| 全局审计 | `ponytail-audit` |
| 界面/UX 改动 | `impeccable`（引擎已装在 `~\.impeccable\bin\0.1.19\impeccable.exe`，先跑 `scripts\impeccable.cmd context --target <文件>`） |
| 设计文档 / 术语 / 决策记录 | `codebase-design`、`domain-modeling` |
| 写用户文档 | `writing-for-agents` |
