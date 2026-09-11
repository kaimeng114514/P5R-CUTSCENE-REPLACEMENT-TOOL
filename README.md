# P5R 过场动画替换工具

**一键解包、预览并替换《女神异闻录 5 皇家版》（PC/Steam）过场动画。**

一个友好的 GUI 工具，自动从 `MOVIE_JE.CPK` 中提取全部过场动画，支持即时预览，并可将任意过场替换为你自己的视频——一键打包成 Reloaded-II 模组。

![版本](https://img.shields.io/badge/version-1.2.1-red)
![平台](https://img.shields.io/badge/platform-Windows%20x64-0078D4)
![游戏](https://img.shields.io/badge/game-Persona%205%20Royal-1B3FBB)
![许可](https://img.shields.io/badge/license-非商业用途-green)
<img width="1280" height="669" alt="屏幕截图 2026-09-11 220123" src="https://github.com/user-attachments/assets/fb83530b-882c-48c2-b4f3-77a5eb1ba9dc" />

---

## 功能特性

- **一键导入 CPK** —— 自动解包 `MOVIE_JE.CPK` 中的全部 103 个 USM 过场
- **即时预览** —— 零重编码，直接播放原始 VP9 视频 + HCA 音轨，点击列表即看
- **双音轨支持** —— 双语过场自动选择日文配音（音轨二）
- **替换任意过场** —— 导入自己的 MP4/MKV/WebM，点一下即可替换
- **批量替换** —— 多个过场同时打包进同一个 Mod
- **Mod 生成器** —— 一键输出可直接用的 Reloaded-II 模组文件夹
- **内置 Reloaded-II** —— 无需单独安装，工具栏一键启动
- **导出 MP4** —— 将任意原过场导出为原始分辨率高质量 MP4（H.264 + AAC）
- **自动记忆会话** —— 下次启动自动恢复 CPK 路径、替换记录和 Mod 输出目录
- **P5 风格 UI** —— 红黑配色、不规则矩形按钮、自定义字体
- **零依赖** —— Python、FFmpeg 和所有库均已内置，双击 exe 即用

---

## 下载与安装

1. 前往 [Releases](../../releases) 页面
2. 下载最新的 `P5R_Cutscene_Tool_vX.X.X.zip`
3. 解压到任意目录（**支持中文路径**）
4. 双击 **`P5R过场动画替换工具.exe`**

> 无需安装 Python、FFmpeg 或任何运行库，全部内置。

---

## 快速上手

### 第 ① 步：导入 CPK

1. 菜单栏：**文件 → 导入 CPK**
2. 选择 `...\Steam\steamapps\common\P5R\CPK\MOVIE_JE.CPK`
3. 等待三阶段进度条：
   - **读取 CPK** → **解包 USM** → **生成预览**
4. 列表显示全部 103 个过场，点击任意一个即可播放。

> 再次启动秒进列表——缓存已就绪，无需重新解包。

### 第 ② 步：预览

- **单击**过场 → 右侧自动加载首帧
- 播放 / 暂停 / 停止 控件在播放器下方
- 无音轨的过场会标注 **"无音轨"**
- 勾选 **"播放替换后视频"** 可对比替换效果
- **导出 MP4…** 可将原过场保存为原始分辨率 MP4

### 第 ③ 步：替换 & 生成 Mod

1. 在列表中选中要替换的过场
2. 在下方待替换预览区点击 **"导入视频…"**
3. 点击 **"替换所选视频"** —— 列表标注为"已替换"
4. 点击 **"一键生成 Mod"**，输入 Mod 名称和简介，选择输出目录
5. 点击 **"启动 Reloaded-II"**，启用你的 Mod，启动游戏即可

> 支持同时替换多个过场，全部打包到同一个 Mod 中。

---

## 常见问题

**Q：第一次导入要多久？**
首次导入需解包约 2-3 GB 的 USM 文件并生成预览缓存，建议用 SSD。再次启动秒进列表。

**Q：替换后游戏内过场没变？**
确认：(1) Reloaded-II 中 Mod 已勾选启用；(2) Mod 文件夹在 Reloaded-II 的 Mods 目录中；(3) 查看 Reloaded-II 日志。

**Q：替换视频能比原版长或短吗？**
可以。工具不会自动加速或裁剪视频。对于时长被硬编码限制的过场（如 MOV001 赌场序幕），工具内置了 FileId Redirector Hook 突破时长限制。

**Q：预览声音不对？**
双语过场（如 MOV001）已默认播放日文配音。如果听到英文，请用最新版本重新导入 CPK。

**Q：杀软报毒？**
PyInstaller 打包的 exe 可能被误报，添加信任即可。

---

## 技术细节

| 项目 | 详情 |
|---|---|
| 容器格式 | CRI USM（CRI Movie 2），位于 `MOVIE_JE.CPK` 内 |
| 加密密钥 | `0x2341683d2fdba6`（PC 版统一） |
| 视频编码 | VP9（IVF，stream 0） |
| 音频编码 | HCA（stream 1+），日文配音 = 最后一条 HCA |
| 替换编码 | VP9 CRF 21（视觉无损），cpu-used 7，realtime |
| 替换音频 | AAC 192 kbps，不重采样 |
| Mod 框架 | Reloaded-II（内置） |

### 已知技术要点

- **中文路径**：底层 CRI 编解码库的 C++ 层不支持非 ASCII 路径。工具通过传入 Python 文件对象而非路径字符串来绕过此限制。
- **GIL 与多线程**：CriCodecs 是 nanobind 编译的 C 扩展，不释放 GIL。预览生成限制为 3 个并发线程以保持 UI 响应。
- **NVENC**：NVIDIA NVENC **不支持** VP9 编码，工具使用 CPU 编码，速度稍慢但画质无损。
- **FileId Hook**：部分过场（特别是 MOV001）有时长硬编码限制。内置的 FileId Redirector 修改了 CRI 文件系统 Hook，允许任意时长的替换视频。

---

## 致谢

- **[ShrineFox](https://shrinefox.com)** —— P5R Mod 制作教程
- **[vgmstream](https://github.com/vgmstream/vgmstream)** —— CRI 音频解码
- **[Reloaded-II](https://github.com/Reloaded-Project/Reloaded-II)** —— Mod 加载框架
- **[lraty-li](https://github.com/lraty-li/Persona-Modding)** —— unpackMovie.py 参考脚本
- **CriCodecs** —— USM/CPK/HCA Python 库
- P5R Modding 社区 Discord

---

## 免责声明

本工具仅供**个人学习与游戏 Mod 制作使用**。游戏文件、过场动画及相关内容版权归 ATLUS / SEGA 所有。本项目与 ATLUS、SEGA、P-Studio 无任何关联。

---

**作者：凯梦**

- B站：https://b23.tv/tIBHJEj
- 抖音：https://www.douyin.com/user/MS4wLjABAAAANIhiC7YSQ6uOCJyK-RUly7hNlrV98mYxxOxHbnCCmKYAGK24aAHNe02L9O1XlchI
