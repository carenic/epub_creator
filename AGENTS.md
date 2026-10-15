# AGENTS.md — 本工作区的工作约定

本工作区（`epub_creator`）用于把**思源笔记导出的 markdown 文件夹**制作成 EPUB 3 电子书。
本文件会被 dsh / Cherry Studio 作为 Workspace Instructions 自动加载，请把它当作硬性约定。

## 核心约定

工作区根目录下同时存在输入与输出，二者同级：

```
<workspace>/
├── Programming.md/     ← 输入：思源导出文件夹（**只读，绝不修改**）
└── Programming.epub    ← 输出：成书（文件名 = 文件夹名去掉 .md）
```

用户只要把导出文件夹放进工作区，就该得到同级的 `.epub`。不要要求用户提供目录截图、
大纲文件或额外的排序信息——目录树必须从文件夹内容还原。

## 构建命令

```bash
# 构建工作区内所有 *.md 文件夹
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py

# 只构建一本 / 清缓存重建 / 构建后校验
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py Programming.md
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py --clean
python3 .agents/skills/siyuan-epub-builder/scripts/build_epub.py --check
```

详细能力、可调项与排错见 skill：`.agents/skills/siyuan-epub-builder/SKILL.md`
（深入参考：同目录 `references/workflow.md`）。**改动构建逻辑时改 skill 里的脚本，不要在工作区根目录另建副本。**

## 目录用途与保留策略

| 路径 | 内容 | 处置 |
| --- | --- | --- |
| `<Book>.md/` | 思源导出输入 | 构建脚本从不写入。构建完成后可移出/归档；**留在原地会导致每次裸跑都重新构建它** |
| `<Book>.epub` | 成书 | 每次构建覆盖。可随时移走副本 |
| `.build/<Book>/` | 解包后的 OEBPS + mermaid PNG 缓存 | 可随时删除，不影响正确性；迭代同一本书时保留可省去重渲 mermaid |
| `.cache/uv/` | Python 依赖缓存（~45 MB） | **保留**，删掉会重新下载约 80 s |
| `.tools/` | 本地 epubcheck + JRE（~192 MB） | **保留**，删掉可用 `scripts/setup_epubcheck.sh` 重装 |
| `~/.cache/puppeteer/` | mermaid 用的 Chrome（~650 MB，全局） | 保留，跨工作区共享 |

归档旧书时把 `X.md/`（连同 `.build/X/`）一起移出工作区即可。**不要把旧书留在工作区根目录下**——
`build_epub.py` 裸跑时会构建所有直接子目录中的 `*.md` 文件夹。放进 `archive/` 之类的子目录则不会被扫描。

## 环境约束（踩过的坑）

- **不要在本会话中途删除或重命名会话工作区目录**。工作区路径消失后，Cherry 的 tool safety
  guard 会对所有工具（含 MCP、bash、read/write）返回
  `The Cherry Studio safety guard could not verify this tool call`，会话直接瘫痪。
  需要改目录名时，让用户新建会话并指定新路径；临时补救是让用户 `ln -s <新目录> <旧路径>`。
- **`uv` 缓存必须指向工作区内**。workspace-write 沙箱下 `~/.cache/uv` 只读，`uv` 会报
  `Could not acquire lock / Read-only file system`。构建脚本已强制 `UV_CACHE_DIR=<cwd>/.cache/uv`。
- **headless Chrome 截图不要加 `--user-data-dir`**（会挂死），且每次调用都要套 `timeout`。
- **冷缓存首次运行慢**（依赖下载 ~80 s，首次 mermaid 还要下 ~650 MB Chrome）。别用短超时，
  否则会误判为卡死。热缓存下一本 21 页的书约 13 s。
- 交付成书前应跑 `.agents/skills/siyuan-epub-builder/scripts/check_epub.sh`，目标是
  `0 fatals / 0 errors / 0 warnings`。
