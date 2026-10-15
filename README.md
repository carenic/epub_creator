# epub_creator

把**思源笔记导出的 markdown 文件夹**制作成一本排版精美的 EPUB 3 电子书。

## 三步

```bash
# 1. 把导出文件夹放进本目录（文件夹名以 .md 结尾）
#    例如 Programming.md/
# 2. 构建
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py
# 3. 成书出现在同级：Programming.md/ → Programming.epub
```

构建后想校验：

```bash
bash .agents/skills/siyuan-epub-builder/scripts/setup_epubcheck.sh   # 只需一次
bash .agents/skills/siyuan-epub-builder/scripts/check_epub.sh
```

> 面向 AI 助手的操作约定、目录用途与保留策略写在 **[AGENTS.md](AGENTS.md)**（会被 Cherry Studio 自动加载）；
> 构建器的完整能力与排错见 **[.agents/skills/siyuan-epub-builder/SKILL.md](.agents/skills/siyuan-epub-builder/SKILL.md)**。
> 本文件只作为人工入口，不重复那里的内容。

## 它做了什么

- 目录与章节顺序**完全从文件夹内容还原**：以索引笔记（如 `Matlab.md`）里的链接顺序为准，
  用同级目录补全遗漏项，层级不限。不需要目录截图或额外的大纲文件。
- 渲染代码高亮、表格（含单元格内 markdown）、任务列表、嵌入图片、
  mermaid 流程图（渲染为 PNG）、LaTeX 公式（转 MathML）、思源彩色标注。
- 生成封面、书名页、嵌套目录页与深色模式样式。
- 只输出 EPUB 3 核心媒体类型，保证 epubcheck 通过（当前 `Programming.epub`：**0 errors / 0 warnings**）。

## 已知的首次运行开销

首次运行需下载 Python 依赖（约 1 分钟）；书中有 mermaid 图时还会下载 Puppeteer 用的
Chrome（约 650 MB，存于 `~/.cache/puppeteer`，全局共享）。之后走缓存，
一本 1.4 MB / 21 页的书约 13 秒构建完成。
