# Book → Skill（候选先验收）

本仓库实现 [Spec #1](https://github.com/lirtual/book-to-skill/issues/1) 的轻量适配流程：使用固定版本的 [virgiliojr94/book-to-skill](https://github.com/virgiliojr94/book-to-skill) 提取书籍，逐块审查**全部可提取文本**，只把可执行且有明确使用边界的方法生成为候选 Skill。候选由用户决定是否安装，默认 **0 个安装、0 个发布**。

本项目不是解析器，也不是自动提炼模型服务。模型分析和候选编写由遵循 [SKILL.md](SKILL.md) 的 Agent 执行，`scripts/book_skill.py` 管理可复核的来源、覆盖、验证与安装门禁。

## 准备（Python 3.10+）

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt
```

依赖从上游官方 GitHub 仓库的精确 commit 安装；运行时还会核对安装来源和 commit，不接受碰巧版本号相同的重新打包构建。默认文本模式可处理简单文本；PDF、EPUB 等格式按上游提示安装**所需的可选依赖**，不会在提取时自动安装缺失依赖。技术 PDF 如需布局提取，可选择 `--mode technical`，其额外依赖及格式限制以固定版上游说明为准。

## 单本书运行示例

确认对输入书籍有处理权限后，选择一个**不在公开仓库、不在宿主 Skills 自动发现目录**中的全新私有路径：

```bash
python scripts/book_skill.py prepare \
  --book /private/books/my-book.epub \
  --workspace /private/reviews/my-book-review \
  --mode text --rights-confirmed
```

`--rights-confirmed` 是明确的权限确认，不代表程序能够判定版权许可。运行使用任务专有的私有目录（0700）、以 0600 权限写入分块文件；发生错误时保留已知位置供诊断或恢复。**不要**把输入、产物路径、原文、候选或检查报告提交到此公开仓库或公共 Issue。

目录结构：

```text
<private-workspace>/
  extracted/full_text.txt      # 上游提取的全文（仅私有）
  extracted/metadata.json      # 来源、指纹、格式与章节元数据
  plan.json                    # 每块起止位置、指纹、审查状态
  blocks/B0001.txt ...         # 全文有序分块（可验证无遗漏/重叠）
  candidates.json              # 由 Agent 审查后编写，可为 0 个
  candidates/<slug>/SKILL.md   # 隔离候选，不自动安装
```

上游有可核验的 PDF 目录章节行范围时按章节组织，并包含未映射的前后文字；否则以有序文本块覆盖全文。**“覆盖提取文本”不等于“完整提取原书”**：未验证的缺页、图片内文字、表格与格式结构始终单独标为 `Unknown`。

## 全书审查与候选登记

1. Agent 按 `plan.json` 的顺序**实际读取每个** `blocks/Bxxxx.txt`，提炼有依据的方法、反例与适用边界。读完一块才记录：

   ```bash
   python scripts/book_skill.py mark --workspace /private/reviews/my-book-review \
     --block B0001 --status checked --note "已读，候选方法与位置已记录"
   ```

2. 对不可读或尚未检查的块维持 `pending`，失败的块记 `failed`；不能声称已完成全书审查。跨块去重，允许零候选。Agent 在私有工作区写 `candidates.json`（结构见下例），为合格候选写隔离的 `SKILL.md`，只在必要时附 `references/` 或脚本。注意引文最小化；不要复制整章。

3. 对每个候选独立测试代表性正例、反例及实际任务，人工逐个检查所有指令性文件的安全性，记录真实结果，然后生成检查报告：

   ```bash
   python scripts/book_skill.py report --workspace /private/reviews/my-book-review
   ```

候选示例（示意结构，不代表真实书籍的候选）：

```json
{
  "candidates": [
    {
      "slug": "example-review-method",
      "purpose": "解决什么具体任务",
      "trigger": "在什么场景下调用",
      "steps": ["可执行的第一步", "可执行的第二步"],
      "boundaries": "不适用与停止条件",
      "sources": ["B0003", "B0011"],
      "evidence": "来源位置及简短可核验依据",
      "positive_task": "适用的测试任务",
      "positive_result": "实际执行回执及结果",
      "negative_task": "不应触发的测试任务",
      "negative_result": "实际执行回执及结果",
      "validation_status": "Pass",
      "security_review": "Pass"
    }
  ]
}
```

如果没有合格方法，使用 `{"candidates": [], "no_candidate_reason": "具体、可核验的原因"}`；不制造候选。验证状态使用 `Pass / Partial / Fail / Unknown`，不能拿静态扫描结果代替人工安全复核。报告失败或不完整不得宣称预验收通过。

## 门禁与恢复

- `report` 的 `review_gate=Pass` 仅代表已登记文本块和候选的**流程预检查**完成；原书提取完整性、模型实际理解和 ChatGPT/Codex 宿主发现/触发均需要各自证据。
- 无论 `report` 输出什么，**程序没有安装操作**。安装某一候选到 ChatGPT 或 Codex 前，必须分别征得用户明确批准；随后按宿主实际原生机制安装、验证触发与移除路径。不通过则保留在隔离目录。
- 中断后已存在 `extracted/metadata.json` 且尚未有 `plan.json` 时，可使用 `python scripts/book_skill.py index --book <原书> --workspace <已有私有目录>` 继续建立审查索引；此操作重新核对原书内容指纹，并复用内容完全一致、权限仍安全的既有分块。冲突、额外文件或不安全路径会停止执行且不覆盖原文件；已有 `plan.json` 不会被覆盖。
- 仅删除**已确认属于本次任务**的私有目录；删除前确认用户不再需要候选和证据，避免不可恢复的数据损失。

## 开发验证

```bash
python3 -m unittest discover -s tests -v
```

此仓库只存放公开的通用流程、代码和合成测试数据；真实书籍端到端验收必须在用户提供有权处理的真实样本后单独进行。
